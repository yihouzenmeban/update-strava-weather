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


if __name__ == "__main__":
    unittest.main()
