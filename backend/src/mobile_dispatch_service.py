"""Mobile (flex) crew dispatch service: schema, gap board, recompute, confirm.

并发与失效口径：

* 每个航站楼一把事务级咨询锁，重排与主管确认互斥串行。
* 重排时把该航站楼所有 pending 派工置为 superseded（旧的待确认占用失效），
  已确认（confirmed）派工原样保留，仍计入保障量。
* 确认在同一把终端锁内二次校验行仍为 pending；等待锁期间若已被重排，
  过期安排返回 409，不能确认。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from fastapi import HTTPException

from . import mobile_dispatch_planner as planner
from .db import audit, connect


def _terminal_lock_key(terminal: str) -> int:
    # 稳定哈希（跨进程一致），收敛到非负 int64 供 pg_advisory_xact_lock 使用
    digest = hashlib.sha256(('mobile-dispatch:' + terminal).encode()).hexdigest()
    return int(digest[:15], 16)

SCHEMA = """
CREATE TABLE IF NOT EXISTS mobile_teams (
  id SERIAL PRIMARY KEY,
  team_code TEXT UNIQUE NOT NULL,
  terminal TEXT NOT NULL,
  capacity INT NOT NULL CHECK (capacity > 0),
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS mobile_dispatch_runs (
  id SERIAL PRIMARY KEY,
  terminal TEXT NOT NULL,
  terminal_version INT NOT NULL,
  trigger TEXT NOT NULL CHECK (trigger IN ('forecast_update','manual')),
  created_by INT REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS mobile_allocations (
  id SERIAL PRIMARY KEY,
  run_id INT NOT NULL REFERENCES mobile_dispatch_runs(id),
  terminal TEXT NOT NULL,
  terminal_version INT NOT NULL,
  team_id INT NOT NULL REFERENCES mobile_teams(id),
  wave_id INT NOT NULL REFERENCES flight_waves(id),
  slot_start TIMESTAMPTZ NOT NULL,
  capacity_bags INT NOT NULL CHECK (capacity_bags > 0),
  allocated_bags NUMERIC(10,2) NOT NULL CHECK (allocated_bags >= 0),
  predicted_bags NUMERIC(10,2) NOT NULL,
  confirmed_coverage NUMERIC(10,2) NOT NULL DEFAULT 0,
  gap_at_dispatch NUMERIC(10,2) NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','confirmed','superseded')),
  confirmed_by INT REFERENCES users(id),
  confirmed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_mobile_live_team_slot
  ON mobile_allocations(team_id, slot_start)
  WHERE status IN ('pending','confirmed');
CREATE INDEX IF NOT EXISTS ix_mobile_wave_slot ON mobile_allocations(wave_id, slot_start);
"""

FORECAST_SQL = """
SELECT f.*, w.code AS wave_code, w.terminal,
       (SELECT max(bc.counted_at) FROM belt_counts bc
         WHERE bc.wave_id = w.id AND bc.status = 'accepted') AS last_counted_at
FROM peak_forecasts f
JOIN flight_waves w ON w.id = f.wave_id
JOIN (
  SELECT wave_id, max(id) AS max_id FROM peak_forecasts
  WHERE status IN ('ready','published')
  GROUP BY wave_id
) latest ON latest.max_id = f.id
WHERE w.status <> 'closed'
ORDER BY w.terminal, f.forecast_at
"""


def ensure_schema(conn):
    conn.execute(SCHEMA)


def _parse_points(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


def _slot_map(last_counted_at, predicted):
    return dict(zip(planner.forecast_slots(last_counted_at, len(predicted)), predicted))


def _load_teams(conn, terminal=None):
    sql = 'SELECT * FROM mobile_teams WHERE active = TRUE'
    params = ()
    if terminal:
        sql += ' AND terminal = %s'
        params = (terminal,)
    sql += ' ORDER BY terminal, id'
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def build_demands(conn, terminal=None):
    """汇总每个 (波次, 档) 的预测件数与已确认保障量，返回纯结构供 planner 使用。"""
    forecasts = [dict(r) for r in conn.execute(FORECAST_SQL).fetchall()]
    if terminal:
        forecasts = [f for f in forecasts if f['terminal'] == terminal]

    slot_rows: dict[tuple, dict] = {}
    for forecast in forecasts:
        last_at = forecast['last_counted_at']
        result = _parse_points(forecast['result'] or {})
        predicted = result.get('predicted_next_4') if isinstance(result, dict) else None
        if not last_at or not predicted:
            continue
        for slot_start, bags in _slot_map(last_at, predicted).items():
            slot_rows[(forecast['wave_id'], slot_start)] = {
                'wave_id': forecast['wave_id'],
                'terminal': forecast['terminal'],
                'slot_start': slot_start,
                'predicted_bags': float(bags),
            }
    if not slot_rows:
        return [], forecasts

    keys = list(slot_rows.keys())
    wave_ids = [w for w, _ in keys]
    slot_starts = [s for _, s in keys]
    # 已确认机动班组保障量（confirmed 派工，重排不会动它们）
    mobile_rows = conn.execute("""
        SELECT a.wave_id, a.slot_start, COALESCE(SUM(a.allocated_bags),0) AS bags,
               array_agg(a.team_id) AS team_ids
        FROM mobile_allocations a
        JOIN unnest(%s::int[], %s::timestamptz[]) AS k(wave_id, slot_start)
          ON k.wave_id = a.wave_id AND k.slot_start = a.slot_start
        WHERE a.status = 'confirmed'
        GROUP BY a.wave_id, a.slot_start
    """, (wave_ids, slot_starts)).fetchall()
    mobile_by_key = {(r['wave_id'], r['slot_start']): r for r in mobile_rows}

    # 已确认人员班次容量（沿用系统口径：capacity*10 件，按档覆盖）
    shift_rows = conn.execute("""
        SELECT s.wave_id, s.slot_start, COALESCE(SUM(s.coverage),0) AS bags FROM (
          SELECT wave_id, generate_series(
                   date_trunc('hour', start_at)
                     + floor(extract(minute FROM start_at)/10)*interval '10 min',
                   end_at - interval '10 min', interval '10 min') AS slot_start,
                 capacity * 10 AS coverage
          FROM staff_shifts
          WHERE status = 'confirmed'
        ) s
        JOIN unnest(%s::int[], %s::timestamptz[]) AS k(wave_id, slot_start)
          ON k.wave_id = s.wave_id AND k.slot_start = s.slot_start
        GROUP BY s.wave_id, s.slot_start
    """, (wave_ids, slot_starts)).fetchall()
    shift_by_key = {(r['wave_id'], r['slot_start']): float(r['bags']) for r in shift_rows}

    demands = []
    for key, base in slot_rows.items():
        mobile = mobile_by_key.get(key)
        mobile_bags = float(mobile['bags']) if mobile else 0.0
        blocked = list(mobile['team_ids']) if mobile and mobile['team_ids'] else []
        shift_bags = shift_by_key.get(key, 0.0)
        confirmed_coverage = mobile_bags + shift_bags
        demands.append({
            **base,
            'confirmed_coverage': confirmed_coverage,
            'gap': base['predicted_bags'] - confirmed_coverage,
            'confirmed_team_ids': blocked,
        })
    demands.sort(key=lambda d: (d['terminal'], d['slot_start'], d['wave_id']))
    return demands, forecasts


def recompute(conn, terminal, user, trigger='manual'):
    """对单个航站楼重算：旧 pending 失效，按新缺口重新派工；confirmed 照旧。"""
    ensure_schema(conn)
    # 终端级咨询锁（稳定哈希到 int64），重排/确认互斥
    lock_key = _terminal_lock_key(terminal)
    conn.execute('SELECT pg_advisory_xact_lock(%s)', (lock_key,))

    teams = _load_teams(conn, terminal)
    demands, forecasts = build_demands(conn, terminal)
    # 旧的待确认占用一律失效；已确认归属保留
    conn.execute("""
        UPDATE mobile_allocations SET status = 'superseded'
        WHERE terminal = %s AND status = 'pending'
    """, (terminal,))
    version_row = conn.execute("""
        SELECT COALESCE(max(terminal_version), 0) + 1 AS next_version
        FROM mobile_allocations WHERE terminal = %s
    """, (terminal,)).fetchone()
    next_version = int(version_row['next_version'])

    run = conn.execute("""
        INSERT INTO mobile_dispatch_runs(terminal, terminal_version, trigger, created_by)
        VALUES (%s,%s,%s,%s) RETURNING *
    """, (terminal, next_version, trigger, user['id'] if user else None)).fetchone()

    plan = planner.allocate(demands, teams)
    for item in plan['allocations']:
        demand = next(d for d in demands
                      if d['wave_id'] == item['wave_id'] and d['slot_start'] == item['slot_start'])
        conn.execute("""
            INSERT INTO mobile_allocations(run_id, terminal, terminal_version, team_id, wave_id,
                slot_start, capacity_bags, allocated_bags, predicted_bags, confirmed_coverage,
                gap_at_dispatch, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending')
        """, (run['id'], terminal, next_version, item['team_id'], item['wave_id'],
              item['slot_start'], item['capacity_bags'], item['allocated_bags'],
              demand['predicted_bags'], demand['confirmed_coverage'], demand['gap']))
    audit(conn, user['id'] if user else None, 'recompute', 'mobile_dispatch', run['id'],
          {'terminal': terminal, 'trigger': trigger,
           'allocations': len(plan['allocations']), 'version': next_version})
    return {'run_id': run['id'], 'terminal': terminal, 'terminal_version': next_version,
            'allocations': plan['allocations'], 'slots': plan['slots'],
            'forecast_count': len(forecasts)}


def confirm_allocation(conn, allocation_id, note, user):
    """主管确认派工；若该派工已被重排置为 superseded，则禁止确认。"""
    ensure_schema(conn)
    row = conn.execute('SELECT * FROM mobile_allocations WHERE id = %s FOR UPDATE',
                       (allocation_id,)).fetchone()
    if not row:
        raise HTTPException(404, '派工记录不存在')
    if row['status'] == 'confirmed':
        raise HTTPException(409, '该派工已经确认')
    if row['status'] == 'superseded':
        raise HTTPException(409, '该安排已在上游预测更新后失效，请按最新缺口重新确认')
    lock_key = _terminal_lock_key(row['terminal'])
    conn.execute('SELECT pg_advisory_xact_lock(%s)', (lock_key,))
    # 拿到锁后再看一次：可能在等待期间已被重排
    fresh = conn.execute('SELECT status FROM mobile_allocations WHERE id = %s',
                         (allocation_id,)).fetchone()
    if fresh['status'] != 'pending':
        raise HTTPException(409, '该安排已在上游预测更新后失效，请按最新缺口重新确认')
    updated = conn.execute("""
        UPDATE mobile_allocations
        SET status = 'confirmed', confirmed_by = %s, confirmed_at = now()
        WHERE id = %s AND status = 'pending' RETURNING *
    """, (user['id'], allocation_id)).fetchone()
    if not updated:
        raise HTTPException(409, '该安排已在上游预测更新后失效，请按最新缺口重新确认')
    audit(conn, user['id'], 'confirm', 'mobile_allocation', allocation_id,
                  {'note': note, 'wave_id': updated['wave_id'],
                   'slot_start': updated['slot_start'].isoformat()})
    return dict(updated)


def board(conn, terminal=None):
    """排班页面：机动班组台账 + 每档缺口 + 派到哪个波次。"""
    ensure_schema(conn)
    teams = _load_teams(conn)
    if terminal:
        teams = [t for t in teams if t['terminal'] == terminal]
    demands, forecasts = build_demands(conn, terminal)
    alloc_sql = """
        SELECT a.*, t.team_code, w.code AS wave_code
        FROM mobile_allocations a
        JOIN mobile_teams t ON t.id = a.team_id
        JOIN flight_waves w ON w.id = a.wave_id
        WHERE a.status IN ('pending','confirmed')
    """
    params = ()
    if terminal:
        alloc_sql += ' AND a.terminal = %s'
        params = (terminal,)
    alloc_sql += ' ORDER BY a.slot_start, a.wave_id, t.team_code'
    live = [dict(r) for r in conn.execute(alloc_sql, params).fetchall()]

    grouped: dict[tuple, list] = {}
    for alloc in live:
        grouped.setdefault((alloc['wave_id'], alloc['slot_start']), []).append(alloc)

    slot_view = []
    for demand in demands:
        entries = grouped.get((demand['wave_id'], demand['slot_start']), [])
        slot_view.append({
            'wave_id': demand['wave_id'],
            'terminal': demand['terminal'],
            'slot_start': demand['slot_start'],
            'predicted_bags': demand['predicted_bags'],
            'confirmed_coverage': demand['confirmed_coverage'],
            'gap': round(demand['gap'], 2),
            'allocations': [{
                'id': e['id'], 'team_id': e['team_id'], 'team_code': e['team_code'],
                'status': e['status'], 'allocated_bags': float(e['allocated_bags']),
                'capacity_bags': e['capacity_bags'],
                'confirmed_by': e['confirmed_by'], 'confirmed_at': e['confirmed_at'],
                'run_id': e['run_id'], 'terminal_version': e['terminal_version'],
            } for e in entries],
        })
    terminals = sorted({t['terminal'] for t in teams} |
                       {d['terminal'] for d in demands})
    return {'teams': teams, 'slots': slot_view, 'terminals': terminals,
            'generated_at': datetime.now(timezone.utc).isoformat()}
