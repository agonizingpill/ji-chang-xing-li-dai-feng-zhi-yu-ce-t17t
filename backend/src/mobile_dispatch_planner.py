"""Pure allocation rules for mobile (flex) crew dispatch.

业务口径（与需求逐条对应）：

* 同一航站楼的波次撞峰时，一个机动班组在同一个十分钟档只能派给一个波次，
  因此不会被重复排。
* 每个档的缺口 = 预测件数 - 本波已确认的保障量（已确认机动班组 + 已确认班次）。
* 每档把可用机动班组优先派给缺口最大的波次，派完即从本档移出，再看下一个波次。
* 上游预测更新后由 service 层把旧的待确认派工置为 superseded，这里只负责
  按“当前缺口 + 当前可用班组”重算，已确认派工不参与重排。

本模块刻意不接触数据库，全部以普通 dict/list 表达，便于用 unittest 复现规则。
"""
from __future__ import annotations

from datetime import datetime, timedelta

SLOT_MINUTES = 10


def next_slot_start(last_counted_at: datetime) -> datetime:
    """末次采样所在十分钟档的下一档起点（向下取整到 10 分钟边界后加一档）。"""
    base = last_counted_at.replace(second=0, microsecond=0)
    boundary = base.replace(minute=base.minute - (base.minute % SLOT_MINUTES))
    # 采样恰好落在档起点时，该档本身就是“末次采样所在档”，下一档仍是 +10 分钟
    return boundary + timedelta(minutes=SLOT_MINUTES)


def forecast_slots(last_counted_at: datetime, point_count: int) -> list[datetime]:
    """从下一档起，按预测点数生成连续的十分钟档起点。"""
    start = next_slot_start(last_counted_at)
    return [start + timedelta(minutes=SLOT_MINUTES * i) for i in range(max(0, point_count))]


def allocate(demands, teams):
    """按档贪心派工。

    demands: 每个元素是一个波次在某档的缺口行，字段：
        wave_id, terminal, slot_start(datetime), predicted_bags,
        confirmed_coverage, gap(数值，可为负),
        confirmed_team_ids(本档已占用班组的 id 集合/列表)
    teams: 机动班组列表，字段 id, team_code, terminal, capacity, active
    返回 {"allocations": [...], "slots": [...]}，slots 为按档汇总的快照。
    """
    active_teams = [t for t in teams if t.get('active', True)]
    by_terminal: dict[str, list] = {}
    for team in active_teams:
        by_terminal.setdefault(team['terminal'], []).append(team)
    for terminal_teams in by_terminal.values():
        # 强班组先上，同容量按编号稳定排序，保证结果可复现
        terminal_teams.sort(key=lambda t: (-int(t['capacity']), t['id']))

    # (terminal, slot_start) -> 该档全部波次行，按时间档排序
    buckets: dict[tuple, list] = {}
    for row in demands:
        buckets.setdefault((row['terminal'], row['slot_start']), []).append(row)

    allocations = []
    slot_summaries = []
    ordered_keys = sorted(buckets, key=lambda key: (key[0], key[1]))
    for terminal, slot_start in ordered_keys:
        rows = sorted(buckets[(terminal, slot_start)],
                      key=lambda r: (-float(r['gap']), r['wave_id']))
        # 同组同档只派一处：班组在本档被派出后即进入 used，后续波次不可再用；
        # 已确认占用在本档对所有撞峰波次全局屏蔽（不只是原波次）
        used: set = set()
        blocked: set = set()
        for row in rows:
            blocked.update(row.get('confirmed_team_ids') or ())
        slot_rows = []
        for row in rows:
            remaining = max(0.0, float(row['gap']))
            assigned_here = []
            for team in by_terminal.get(terminal, []):
                if remaining <= 0:
                    break
                if team['id'] in used or team['id'] in blocked:
                    continue
                used.add(team['id'])
                capacity = int(team['capacity'])
                allocated_bags = min(capacity, remaining)
                remaining -= allocated_bags
                record = {
                    'team_id': team['id'],
                    'team_code': team['team_code'],
                    'wave_id': row['wave_id'],
                    'terminal': terminal,
                    'slot_start': slot_start,
                    'capacity_bags': capacity,
                    'allocated_bags': round(allocated_bags, 2),
                }
                allocations.append(record)
                assigned_here.append(record)
            slot_rows.append({
                'wave_id': row['wave_id'],
                'terminal': terminal,
                'slot_start': slot_start,
                'predicted_bags': float(row['predicted_bags']),
                'confirmed_coverage': float(row['confirmed_coverage']),
                'gap_before': float(row['gap']),
                'gap_remaining': round(remaining, 2),
                'assigned': assigned_here,
            })
        slot_summaries.append({'terminal': terminal, 'slot_start': slot_start, 'waves': slot_rows})
    return {'allocations': allocations, 'slots': slot_summaries}
