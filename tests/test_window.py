"""每日测试窗口判定：普通窗口、跨日窗口、全天窗口。"""
import unittest
from datetime import datetime, time, timezone

from road_permits.domain import interval_in_window, time_in_window

UTC = timezone.utc


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


class TimeInWindowTests(unittest.TestCase):
    def test_normal_window(self):
        self.assertTrue(time_in_window(time(9, 0), time(9, 0), time(17, 0)))
        self.assertTrue(time_in_window(time(16, 59), time(9, 0), time(17, 0)))
        self.assertFalse(time_in_window(time(17, 0), time(9, 0), time(17, 0)))
        self.assertFalse(time_in_window(time(8, 59), time(9, 0), time(17, 0)))

    def test_cross_day_window(self):
        start, end = time(22, 0), time(2, 0)
        self.assertTrue(time_in_window(time(22, 0), start, end))
        self.assertTrue(time_in_window(time(23, 30), start, end))
        self.assertTrue(time_in_window(time(0, 30), start, end))
        self.assertTrue(time_in_window(time(1, 59), start, end))
        self.assertFalse(time_in_window(time(2, 0), start, end))
        self.assertFalse(time_in_window(time(12, 0), start, end))
        self.assertFalse(time_in_window(time(21, 59), start, end))

    def test_full_day_window(self):
        self.assertTrue(time_in_window(time(3, 0), time(0, 0), time(0, 0)))


class IntervalInWindowTests(unittest.TestCase):
    def test_interval_inside_normal_window(self):
        self.assertTrue(interval_in_window(at(7, 10), at(7, 16, 30), time(9), time(17)))
        self.assertFalse(interval_in_window(at(7, 8), at(7, 10), time(9), time(17)))
        self.assertFalse(interval_in_window(at(7, 16), at(7, 18), time(9), time(17)))

    def test_interval_inside_cross_day_window(self):
        start, end = time(22), time(2)
        # 完全落在夜间跨日窗口内
        self.assertTrue(interval_in_window(at(7, 23), at(8, 1), start, end))
        self.assertTrue(interval_in_window(at(8, 0, 30), at(8, 1, 30), start, end))
        # 起点在窗口开始前
        self.assertFalse(interval_in_window(at(7, 21, 30), at(7, 23), start, end))
        # 终点越过窗口结束
        self.assertFalse(interval_in_window(at(8, 1), at(8, 3), start, end))
        # 白天时段完全在禁区
        self.assertFalse(interval_in_window(at(7, 12), at(7, 13), start, end))
        # 跨越多日的长区间必然穿过禁区
        self.assertFalse(interval_in_window(at(7, 23), at(9, 1), start, end))

    def test_full_day_window_allows_any_interval(self):
        self.assertTrue(interval_in_window(at(7, 3), at(8, 5), time(0), time(0)))


if __name__ == "__main__":
    unittest.main()
