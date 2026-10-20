#!/usr/bin/env python3
"""Exercise offline peak matching and peak-line description updates."""

import importlib.util
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("peaks", ROOT / "peaks.py")
peaks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peaks)

# Synthetic peak DB: two named peaks + one unnamed, near (10.0, 20.0).
PEAKS = [
    (1, "测试峰", 500.0, 10.001, 20.0),
    (2, "第二峰", None, 10.005, 20.002),
    (3, "未命名", 300.0, 10.002, 20.001),
    (4, "", 100.0, 10.003, 20.0),
]


def make_db():
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / "test.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE osm_peak (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                 "elevation_m REAL, lat REAL NOT NULL, lon REAL NOT NULL)")
    conn.executemany("INSERT INTO osm_peak VALUES (?,?,?,?,?)", PEAKS)
    conn.commit()
    conn.close()
    return [path]


def track_through(lat, lon, n=400, span=0.01):
    """Straight north-south track passing through (lat, lon)."""
    return [(lat - span / 2 + span * i / (n - 1), lon) for i in range(n)]


class MatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = make_db()

    def test_trailrun_matches_within_100m(self):
        pts = track_through(10.001, 20.0)
        matched = peaks.match_peaks(pts, "TrailRun", db_paths=self.db)
        names = [m[0] for m in matched]
        self.assertIn("测试峰", names)
        # 未命名 / 空名不过滤不展示
        self.assertNotIn("未命名", names)
        self.assertNotIn("", names)

    def test_ride_does_not_participate(self):
        pts = track_through(10.001, 20.0)
        self.assertEqual(peaks.match_peaks(pts, "Ride", db_paths=self.db), [])

    def test_run_uses_50m_radius(self):
        # 70m 外经过：TrailRun(100m) 命中，Run(50m) 不命中
        pts = track_through(10.001, 20.0007)  # ~70m east
        trail = [m[0] for m in peaks.match_peaks(pts, "TrailRun", db_paths=self.db)]
        run = [m[0] for m in peaks.match_peaks(pts, "Run", db_paths=self.db)]
        self.assertIn("测试峰", trail)
        self.assertNotIn("测试峰", run)

    def test_repeat_pass_counts_twice(self):
        pts = track_through(10.001, 20.0, n=200)
        # 远离 1500m 再回来
        pts += [(10.001 + 0.015, 20.0 + i * 0.0001) for i in range(200)]
        pts += track_through(10.001, 20.0, n=200)
        matched = dict((m[0], m[2]) for m in peaks.match_peaks(pts, "TrailRun", db_paths=self.db))
        self.assertEqual(matched.get("测试峰"), 2)

    def test_format_line(self):
        line = peaks.format_peak_line([("大阳台", 587.0, 2), ("小阳台", 465.0, 1), ("无名峰", None, 1)])
        self.assertEqual(line, "⛰️ 大阳台 (587 m) ×2 • ⛰️ 小阳台 (465 m) • ⛰️ 无名峰")


class SamplingTest(unittest.TestCase):
    def test_distance_sample_50_per_km(self):
        # 10m 间隔的直线轨迹约 1km，20m 采样应得到 ~50 个点
        pts = [(10.0 + i * 0.0001, 20.0) for i in range(101)]  # ~10.0m each
        sampled = peaks.distance_sample(pts, 20.0)
        self.assertTrue(45 <= len(sampled) <= 56, f"got {len(sampled)}")
        self.assertEqual(sampled[0], pts[0])
        self.assertEqual(sampled[-1], pts[-1])

    def test_distance_sample_short_track(self):
        pts = [(10.0, 20.0), (10.0001, 20.0)]  # ~11m
        self.assertEqual(peaks.distance_sample(pts, 20.0), pts)

    def test_distance_sample_skips_duplicate_points(self):
        pts = [(10.0, 20.0)] * 50 + [(10.0005, 20.0)]  # 重复点不触发采样
        sampled = peaks.distance_sample(pts, 20.0)
        self.assertEqual(sampled, [(10.0, 20.0), (10.0005, 20.0)])


class DescriptionTest(unittest.TestCase):
    WEATHER = ("温度: 10.00°C - 12.00°C\n湿度: 60.00% - 70.00%\n"
               "Not Powered By www.gearaut.com 🍚")

    def test_peak_line_goes_before_weather(self):
        desc = f"我的备注\n\n{self.WEATHER}"
        result = peaks.update_peak_line(desc, "⛰️ 测试峰 (500 m)")
        self.assertLess(result.index("⛰️ 测试峰"), result.index("温度:"))
        self.assertIn("我的备注", result)
        self.assertIn(self.WEATHER, result)

    def test_old_summitbag_removed(self):
        desc = ("备注\n⛰️ 老峰 (100 m)\n🌐 https://summitbag.com/x\n"
                f"2025年累计爬升 999 m\n{self.WEATHER}")
        result = peaks.update_peak_line(desc, "⛰️ 测试峰 (500 m)")
        self.assertNotIn("summitbag", result)
        self.assertNotIn("累计爬升", result)
        self.assertNotIn("老峰", result)
        self.assertIn("⛰️ 测试峰 (500 m)", result)

    def test_idempotent(self):
        desc = f"备注\n\n{self.WEATHER}"
        once = peaks.update_peak_line(desc, "⛰️ 测试峰 (500 m)")
        twice = peaks.update_peak_line(once, "⛰️ 测试峰 (500 m)")
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("⛰️ 测试峰"), 1)

    def test_no_peaks_no_new_line(self):
        desc = f"备注\n⛰️ 老峰 (100 m)\n{self.WEATHER}"
        result = peaks.update_peak_line(desc, "")
        self.assertNotIn("⛰️", result)
        self.assertIn("备注", result)
        self.assertIn(self.WEATHER, result)

    def test_no_weather_appends_after_notes(self):
        result = peaks.update_peak_line("只有备注", "⛰️ 测试峰 (500 m)")
        self.assertTrue(result.index("只有备注") < result.index("⛰️ 测试峰"))

    def test_summitbag_at_top_leaves_no_leading_blank_line(self):
        # 描述第一行就是 summitbag 峰行（前面没有用户备注）：清掉后峰行直接开头
        desc = f"⛰️ 老峰 (100 m) | 🌐 summitbag.com\n{self.WEATHER}"
        result = peaks.update_peak_line(desc, "⛰️ 新峰 (600 m)")
        self.assertTrue(result.startswith("⛰️ 新峰 (600 m)"), repr(result[:30]))

    def test_peak_line_on_empty_description_has_no_blank_line(self):
        result = peaks.update_peak_line("", "⛰️ 新峰 (600 m)")
        self.assertEqual(result, "⛰️ 新峰 (600 m)")


if __name__ == "__main__":
    unittest.main()
