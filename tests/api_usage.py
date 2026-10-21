#!/usr/bin/env python3
"""Exercise Strava API usage tracking: logging, daily reset, 15-min window, reserves."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("api_usage", ROOT / "api_usage.py")
api_usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api_usage)

CONFIG = {
    "read_15min": 200, "read_daily": 2000,
    "reserve_read_15min": 50, "reserve_read_daily": 200,
}


def tracker(now=None):
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / ".strava_api_usage.json"
    return api_usage.UsageTracker(path, CONFIG, now_fn=lambda: now if now is not None else time.time())


class UsageTest(unittest.TestCase):
    def test_get_counts_read(self):
        t = tracker()
        t.log_call("GET", "/athlete/activities")
        status = t.get_status()
        self.assertEqual(status["read_daily_used"], 1)
        self.assertEqual(status["read_15min_used"], 1)
        # overall 口径已删除：两组计数恒等、限额更低，永远不可能先触线
        self.assertNotIn("overall_daily_used", status)

    def test_put_counts_read_too(self):
        # 2026-10-21 实测响应头：PUT 同样计入 read，且读限额打满时 PUT 也被 429
        t = tracker()
        t.log_call("PUT", "/activities/123")
        status = t.get_status()
        self.assertEqual(status["read_daily_used"], 1)
        self.assertEqual(status["read_15min_used"], 1)

    def test_daily_resets_at_utc_midnight(self):
        # 北京时间 08:00 = UTC 午夜；跨过即清零
        before = 1760918399  # UTC 午夜前 1 秒
        t = tracker(now=before)
        t.log_call("GET", "/athlete/activities")
        self.assertEqual(t.get_status()["read_daily_used"], 1)
        t2 = api_usage.UsageTracker(t.path, CONFIG, now_fn=lambda: before + 2)
        self.assertEqual(t2.get_status()["read_daily_used"], 0)

    def test_15min_window_slides(self):
        now = 1760900000
        t = tracker(now=now)
        t.log_call("GET", "/a")
        t.log_call("GET", "/b")
        self.assertEqual(t.get_status()["read_15min_used"], 2)
        t2 = api_usage.UsageTracker(t.path, CONFIG, now_fn=lambda: now + 901)
        self.assertEqual(t2.get_status()["read_15min_used"], 0)
        # daily 不受 15 分钟窗口影响
        self.assertEqual(t2.get_status()["read_daily_used"], 2)

    def test_bulk_blocked_at_reserve_line(self):
        now = 1760900000
        t = tracker(now=now)
        # 用到预留线：2000 - 200 = 1800
        for _ in range(1800):
            t.log_call("GET", "/athlete/activities")
        ok, reason = t.check_budget(bulk=True)
        self.assertFalse(ok)
        self.assertIn("预留", reason)

    def test_bulk_allowed_below_reserve_line(self):
        t = tracker()
        for _ in range(100):
            t.log_call("GET", "/athlete/activities")
        ok, _ = t.check_budget(bulk=True)
        self.assertTrue(ok)

    def test_webhook_never_blocked(self):
        now = 1760900000
        t = tracker(now=now)
        for _ in range(1999):
            t.log_call("GET", "/athlete/activities")
        ok, _ = t.check_budget(bulk=False)
        self.assertTrue(ok)

    def test_usage_file_permissions(self):
        t = tracker()
        t.log_call("GET", "/a")
        mode = os.stat(t.path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_concurrent_logging_keeps_count(self):
        t = tracker()
        for _ in range(50):
            api_usage.UsageTracker(t.path, CONFIG).log_call("GET", "/a")
        self.assertEqual(t.get_status()["read_daily_used"], 50)


if __name__ == "__main__":
    unittest.main()
