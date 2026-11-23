"""Unit tests for mobile dispatch rules. Run: python -m unittest discover -s backend/tests

仅依赖标准库与纯领域模块；服务层的失效/确认口径用内存状态机复刻，
不要求 PostgreSQL 或第三方包。
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mobile_dispatch_planner import allocate, forecast_slots, next_slot_start  # noqa: E402

UTC = timezone.utc
T = datetime(2026, 11, 21, 8, 0, tzinfo=UTC)


def slot(minute):
    return T + timedelta(minutes=minute)


def demand(wave_id, minute, predicted, coverage=0.0, confirmed_teams=None, terminal='T2'):
    return {
        'wave_id': wave_id, 'terminal': terminal,
        'slot_start': slot(minute), 'predicted_bags': float(predicted),
        'confirmed_coverage': float(coverage),
        'gap': float(predicted) - float(coverage),
        'confirmed_team_ids': confirmed_teams or [],
    }


def team(tid, code, capacity, terminal='T2', active=True):
    return {'id': tid, 'team_code': code, 'terminal': terminal,
            'capacity': capacity, 'active': active}


class SlotBoundaryTests(unittest.TestCase):
    def test_next_slot_rounds_up_to_next_ten_minute_bucket(self):
        # 末次采样 08:14 落在 08:10 档，下一档是 08:20
        self.assertEqual(next_slot_start(T.replace(minute=14)), T.replace(minute=20))

    def test_next_slot_when_exactly_on_boundary(self):
        # 恰在整点（08:10 档的起点），下一档仍是 08:20
        self.assertEqual(next_slot_start(T.replace(minute=10)), T.replace(minute=20))

    def test_forecast_slots_are_consecutive_ten_minute_buckets(self):
        slots = forecast_slots(T.replace(minute=14), 4)
        self.assertEqual(slots, [T.replace(minute=20), T.replace(minute=30),
                                 T.replace(minute=40), T.replace(minute=50)])


class AllocationRuleTests(unittest.TestCase):
    def setUp(self):
        self.teams = [team(1, 'FLEX-A', 60), team(2, 'FLEX-B', 45), team(3, 'FLEX-C', 30)]

    def test_same_team_only_dispatched_once_per_slot_even_with_clashing_waves(self):
        # 两个 T2 波次在同一档撞峰，都有缺口
        demands = [demand(101, 0, 100), demand(102, 0, 90)]
        result = allocate(demands, self.teams)
        assigned_team_ids = [a['team_id'] for a in result['allocations']
                             if a['slot_start'] == slot(0)]
        # 三个班组各自只出现一次，没有被重复排
        self.assertEqual(sorted(assigned_team_ids), [1, 2, 3])

    def test_largest_gap_wave_gets_priority_each_slot(self):
        demands = [demand(101, 0, 100), demand(102, 0, 200)]
        result = allocate(demands, self.teams)
        first = result['allocations'][0]
        # 缺口最大的 102 先拿到最强班组
        self.assertEqual((first['wave_id'], first['team_id']), (102, 1))

    def test_allocation_stops_when_gap_covered_and_spills_to_next_wave(self):
        # 101 缺口 80 先被服务：FLEX-A(60) 补到剩 20，FLEX-B 再补 20；
        # 随后 FLEX-C(30) 才转去缺口 50 的 102，补 30 后 102 仍剩 20
        demands = [demand(101, 0, 80), demand(102, 0, 50)]
        result = allocate(demands, self.teams)
        by_wave = {}
        for a in result['allocations']:
            by_wave.setdefault(a['wave_id'], []).append(a)
        self.assertEqual([a['team_id'] for a in by_wave[101]], [1, 2])
        self.assertEqual(by_wave[101][1]['allocated_bags'], 20)
        self.assertEqual([a['team_id'] for a in by_wave[102]], [3])
        wave_102 = next(s for s in result['slots'][0]['waves'] if s['wave_id'] == 102)
        self.assertEqual(wave_102['gap_remaining'], 20)

    def test_gap_remaining_when_not_enough_teams(self):
        # 总运力 60+45+30=135，缺口 140，最后仍剩 5
        result = allocate([demand(101, 0, 140)], self.teams)
        self.assertEqual(result['slots'][0]['waves'][0]['gap_remaining'], 5)

    def test_confirmed_coverage_reduces_gap_before_dispatch(self):
        # 预测 100，已确认保障 70 → 缺口只有 30，一个班组即可
        demands = [demand(101, 0, 100, coverage=70)]
        result = allocate(demands, self.teams)
        self.assertEqual(len(result['allocations']), 1)
        self.assertEqual(result['allocations'][0]['allocated_bags'], 30)

    def test_team_blocked_by_confirmed_allocation_in_same_slot(self):
        # FLEX-A 已在本档确认给 101，重算时不能再派给撞峰的 102
        demands = [demand(101, 0, 60, coverage=60, confirmed_teams=[1]),
                   demand(102, 0, 200)]
        result = allocate(demands, self.teams)
        self.assertNotIn(1, [a['team_id'] for a in result['allocations']
                             if a['wave_id'] == 102])
        self.assertEqual(sorted(a['team_id'] for a in result['allocations']
                                if a['wave_id'] == 102), [2, 3])

    def test_teams_reusable_across_different_slots(self):
        demands = [demand(101, 0, 60), demand(101, 10, 60)]
        result = allocate(demands, self.teams)
        # 同一班组在不同档各出现一次是允许的
        per_slot = {s['slot_start']: [a['team_id'] for a in s['waves'][0]['assigned']]
                    for s in result['slots']}
        self.assertEqual(per_slot[slot(0)], [1])
        self.assertEqual(per_slot[slot(10)], [1])

    def test_inactive_and_other_terminal_teams_excluded(self):
        teams = [team(1, 'A', 60, active=False), team(2, 'B', 45, terminal='T3')]
        result = allocate([demand(101, 0, 100)], teams)
        self.assertEqual(result['allocations'], [])


class StaleConfirmationStateMachineTests(unittest.TestCase):
    """复刻服务层状态机：重排使 pending 失效、confirmed 保留、过期安排不可确认。"""

    def setUp(self):
        self.teams = [team(1, 'FLEX-A', 60), team(2, 'FLEX-B', 45)]
        # 状态：allocation_id -> dict(status, wave, slot)
        self.allocations = {}
        self.next_id = 1
        self.version = 0

    def recompute(self, demands):
        # 旧的待确认占用失效
        for row in self.allocations.values():
            if row['status'] == 'pending':
                row['status'] = 'superseded'
        self.version += 1
        plan = allocate(demands, self.teams)
        for item in plan['allocations']:
            self.allocations[self.next_id] = {
                'status': 'pending', 'version': self.version,
                'wave_id': item['wave_id'], 'slot': item['slot_start'],
                'team_id': item['team_id']}
            self.next_id += 1

    def confirm(self, allocation_id):
        row = self.allocations.get(allocation_id)
        if row is None:
            raise KeyError('not found')
        if row['status'] == 'superseded':
            raise Conflict('该安排已在上游预测更新后失效，不能确认')
        if row['status'] == 'confirmed':
            raise Conflict('已经确认')
        row['status'] = 'confirmed'

    def test_forecast_update_invalidates_pending_but_keeps_confirmed(self):
        self.recompute([demand(101, 0, 100), demand(102, 0, 90)])
        first_pending = next(k for k, v in self.allocations.items() if v['status'] == 'pending')
        self.confirm(first_pending)  # 主管先确认其中一条

        # 上游预测更新 → 重排
        self.recompute([demand(101, 0, 130), demand(102, 0, 60)])

        # 已确认的归属照旧
        self.assertEqual(self.allocations[first_pending]['status'], 'confirmed')
        # 重排同时存在旧 superseded 与新 pending
        self.assertIn('superseded', {v['status'] for v in self.allocations.values()})
        self.assertIn('pending', {v['status'] for v in self.allocations.values()})

        # 过期安排不能确认
        stale = next(k for k, v in self.allocations.items() if v['status'] == 'superseded')
        with self.assertRaises(Conflict):
            self.confirm(stale)

    def test_no_double_booking_after_confirm_then_recompute(self):
        # 首档 FLEX-A 确认给 101，重排时该档仍被 101 占用，102 不能再拿到 FLEX-A
        self.recompute([demand(101, 0, 60), demand(102, 0, 60)])
        flex_a_first = next(k for k, v in self.allocations.items()
                            if v['team_id'] == 1 and v['slot'] == slot(0))
        self.confirm(flex_a_first)
        blocked = [demand(101, 0, 60, coverage=60, confirmed_teams=[1]),
                   demand(102, 0, 60)]
        self.recompute(blocked)
        for row in self.allocations.values():
            if row['status'] == 'pending' and row['slot'] == slot(0):
                self.assertNotEqual(row['team_id'], 1)


class Conflict(Exception):
    pass


if __name__ == '__main__':
    unittest.main()
