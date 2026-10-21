"""Track Strava API call volume against rate limits, reserving headroom for webhook.

Every data-API call is logged with timestamp + method. Two counters are kept:
- daily counters, reset at Beijing 08:00 (UTC midnight)
- 15-minute sliding window

Counting model (verified 2026-10-21 against live response headers): Strava
increments BOTH the read and the overall counters for every data-API call,
whatever the method — x-readratelimit-usage always equals x-ratelimit-usage,
and once the read window is exhausted even PUT requests are rejected with
429. So every logged call counts toward both counters here too; the read
limits (200/15min, 2000/day) are the effective ceiling for all calls.

Bulk operations (backfill, batch mute, ...) must pass check_budget() before
starting and during processing; the webhook path (--activity-id) is exempt.
"""

import datetime as dt
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
USAGE_FILE = ROOT / ".strava_api_usage.json"
WINDOW_SECONDS = 900


class UsageTracker:
    def __init__(self, path=None, config=None, now_fn=None):
        self.path = Path(path) if path is not None else USAGE_FILE
        self.config = config or self._load_config()
        self._now = now_fn

    def _load_config(self):
        sys.path.insert(0, str(ROOT))
        from project_config import load_config
        return load_config(ROOT)["rate_limit"]

    def _now_ts(self):
        import time
        return self._now() if self._now else time.time()

    def _utc_date(self, ts):
        # Strava 配额按 UTC 午夜重置（= 北京时间 08:00）
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()

    def _empty(self, today):
        return {"daily_date": today, "read_daily": 0, "overall_daily": 0, "window": []}

    def _load(self, now):
        today = self._utc_date(now)
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return self._empty(today), None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                with os.fdopen(os.dup(fd), "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (ValueError, OSError):
                data = None
            if not isinstance(data, dict) or data.get("daily_date") != today:
                data = self._empty(today)
            data.setdefault("read_daily", 0)
            data.setdefault("overall_daily", 0)
            data.setdefault("window", [])
            # prune 15-min window: [ts, is_read]
            cutoff = now - WINDOW_SECONDS
            data["window"] = [e for e in data["window"]
                              if isinstance(e, list) and len(e) == 2 and e[0] > cutoff]
            return data, fd
        except OSError:
            os.close(fd)
            return self._empty(today), None

    def _save(self, data, fd):
        if fd is None:
            return
        try:
            os.fchmod(fd, 0o600)
            # atomic replace through temp file
            tmp_fd, tmp_path = tempfile.mkstemp(prefix=".strava_api_usage.", dir=self.path.parent)
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as out:
                    json.dump(data, out)
                    out.flush()
                    os.fsync(out.fileno())
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, self.path)
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def log_call(self, method, endpoint=""):
        now = self._now_ts()
        # 每个调用同时计入 read 与 overall（见模块 docstring 的实测结论）。
        # window 条目的 is_read 仅为兼容旧记量文件而保留，恒为 True。
        data, fd = self._load(now)
        try:
            data["window"].append([now, True])
            data["overall_daily"] += 1
            data["read_daily"] += 1
        finally:
            self._save(data, fd)

    def get_status(self):
        now = self._now_ts()
        data, fd = self._load(now)
        try:
            read_15 = sum(1 for _, r in data["window"] if r)
            overall_15 = len(data["window"])
            cfg = self.config
            return {
                "read_15min_used": read_15, "read_15min_limit": cfg["read_15min"],
                "read_15min_reserve": cfg["reserve_read_15min"],
                "read_daily_used": data["read_daily"], "read_daily_limit": cfg["read_daily"],
                "read_daily_reserve": cfg["reserve_read_daily"],
                "overall_15min_used": overall_15, "overall_15min_limit": cfg["overall_15min"],
                "overall_15min_reserve": cfg["reserve_overall_15min"],
                "overall_daily_used": data["overall_daily"], "overall_daily_limit": cfg["overall_daily"],
                "overall_daily_reserve": cfg["reserve_overall_daily"],
            }
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def check_budget(self, bulk=True):
        """Bulk operations must stay out of the webhook reserve.

        Returns (ok, reason). The webhook path (bulk=False) is never blocked.
        """
        if not bulk:
            return True, ""
        s = self.get_status()
        checks = [
            ("read_15min", "15分钟读取"),
            ("read_daily", "每日读取"),
            ("overall_15min", "15分钟总计"),
            ("overall_daily", "每日总计"),
        ]
        for key, label in checks:
            if s[f"{key}_used"] >= s[f"{key}_limit"] - s[f"{key}_reserve"]:
                return False, (f"{label}已用 {s[f'{key}_used']}/{s[f'{key}_limit']}，"
                               f"触及 webhook 预留额度（{s[f'{key}_reserve']}），批量操作暂停")
        return True, ""


def format_status(status):
    def line(label, key):
        used, limit, reserve = status[f"{key}_used"], status[f"{key}_limit"], status[f"{key}_reserve"]
        avail = max(0, limit - reserve - used)
        return f"{label}: {used}/{limit}（预留 {reserve}，可用 {avail}）"
    return "\n".join([
        line("read    15分钟", "read_15min"),
        line("read    今天  ", "read_daily"),
        line("overall 15分钟", "overall_15min"),
        line("overall 今天  ", "overall_daily"),
    ])


def main(argv):
    if len(argv) < 2:
        print("用法: api_usage.py log <METHOD> <PATH> | status | check", file=sys.stderr)
        return 2
    tracker = UsageTracker()
    command = argv[1]
    if command == "log":
        if len(argv) != 4:
            print("用法: api_usage.py log <METHOD> <PATH>", file=sys.stderr)
            return 2
        tracker.log_call(argv[2], argv[3])
        return 0
    if command == "status":
        print(format_status(tracker.get_status()))
        return 0
    if command == "check":
        ok, reason = tracker.check_budget(bulk=True)
        if not ok:
            print(f"⏸️ {reason}", file=sys.stderr)
        return 0 if ok else 1
    print(f"未知命令: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
