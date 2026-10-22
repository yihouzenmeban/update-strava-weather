#!/usr/bin/env python3
"""Exercise sync with synthetic activities and no external API calls."""

import datetime as dt
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BLOCK = ("温度: 20.00°C - 22.00°C  体感温度: 30.00°C - 32.00°C\n"
         "湿度: 60.00% - 70.00%  风速: 3.00 km/h - 5.00 km/h\nPowered By www.gearaut.com")
MOUNTAINS = "⛰️ 馒头峪 (767 m) • ⛰️ 萝芭地北尖 (1,146 m) | 🌐 summitbag.com"

CURL_MOCK = r'''#!/usr/bin/env python3
import datetime as dt
import json, sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
args = sys.argv[1:]
assert sys.stdin.read().strip() == "Authorization: Bearer test-access-token"
url = next(arg for arg in args if arg.startswith("https://"))
assert url.startswith("https://www.strava.com/api/v3/")
method = args[args.index("-X") + 1] if "-X" in args else "GET"
scenario = json.loads(Path("scenario.json").read_text())
log = Path("requests.jsonl")
entry = {"kind": "strava", "method": method, "url": url}
status = "200"
parsed = urlsplit(url)
query = parse_qs(parsed.query)
activity_id = parsed.path.split("/activities/")[-1].split("/")[0]
dataset = scenario.get("activities", [scenario["activity"]])
details = {str(item["id"]): item for item in dataset}
details.update(scenario.get("details", {}))
if method == "PUT":
    body = json.loads(Path(args[args.index("--data-binary") + 1][1:]).read_text())
    entry["body"] = body
    status = str(scenario.get("put_status_by_id", {}).get(activity_id, scenario.get("put_status", 200)))
    payload = dict(body, id=scenario.get("put_id", int(activity_id)))
elif "/athlete/activities?" in url:
    page = int(query.get("page", [1])[0])
    count = int(query.get("per_page", [1])[0])
    status = str(scenario.get("list_status_by_page", {}).get(str(page), 200))
    if "pages" in scenario:
        payload = scenario["pages"][page - 1] if page <= len(scenario["pages"]) else []
    else:
        after = int(query.get("after", [0])[0])
        before = int(query.get("before", [253402300799])[0])
        dataset = [item for item in dataset if after < dt.datetime.fromisoformat(
            item["start_date"].replace("Z", "+00:00")).timestamp() < before]
        payload = [] if scenario.get("empty") else dataset[(page - 1) * count:page * count]
elif "/streams?" in url:
    payload = scenario.get("streams", {"latlng": {"data": [None, [39.9, 116.4]]}})
    status = str(scenario.get("streams_status", 200))
else:
    payload = details[activity_id]
    status = str(scenario.get("get_status_by_id", {}).get(activity_id, 200))
Path(args[args.index("-o") + 1]).write_text(json.dumps(payload))
with log.open("a") as output:
    output.write(json.dumps(entry) + "\n")
print(status, end="")
'''

WEATHER_MOCK = r'''
import json, urllib.error, urllib.request
from pathlib import Path
class Response:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return json.dumps(self.payload).encode()
def urlopen(request, timeout=None):
    url = request if isinstance(request, str) else request.full_url
    assert url.startswith(("https://api.open-meteo.com/", "https://archive-api.open-meteo.com/")), url
    with open("requests.jsonl", "a") as log:
        log.write(json.dumps({"kind": "weather", "url": url}) + "\n")
    scenario = json.loads(Path("scenario.json").read_text())
    if scenario.get("weather_error"):
        raise urllib.error.URLError("synthetic network failure")
    return Response(scenario["weather"])
urllib.request.urlopen = urlopen
'''


class SyncFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="strava-sync-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        for name in ("update_strava_weather.sh", "strava_common.sh", "strava_sync.py", "weather_template.py",
                     "project_config.py", "config.default.json", "peaks.py"):
            if (ROOT / name).exists():
                shutil.copy2(ROOT / name, self.directory)
        self.config = {"strava": {"client_id": "test-client-id", "client_secret": "test-client-secret"}}
        (self.directory / "config.local.json").write_text(json.dumps(self.config))
        (self.directory / ".strava_tokens.json").write_text(json.dumps({
            "access_token": "test-access-token", "refresh_token": "test-refresh-token", "expires_at": 4102444800,
        }))
        self.runtime = self.directory / "runtime"
        self.runtime.mkdir()
        (self.directory / "bin").mkdir()
        curl = self.directory / "bin/curl"
        curl.write_text(CURL_MOCK)
        curl.chmod(0o755)
        (self.directory / "sitecustomize.py").write_text(WEATHER_MOCK)
        self.environment = os.environ.copy()
        for name in ("ACTIVITY_ID", "YES", "DRY_RUN", "FORCE", "WEATHER_DESC", "WEATHER_DESC_FILE",
                     "WEATHER_EMOJI", "WEATHER_EMOJI_FILE", "WEATHER_TEMPLATE_FILE", "REORDER_SUMMITBAG", "MAP_STYLE",
                     "STRAVA_COMMUTE_FIX",
                     "STRAVA_SYNC_ACTIVITY_FILE", "STRAVA_REORDER_ONLY", "CLIENT_ID", "CLIENT_SECRET"):
            self.environment.pop(name, None)
        self.environment.update(PATH=f'{self.directory / "bin"}:{os.environ["PATH"]}',
                                PYTHONPATH=str(self.directory), TMPDIR=str(self.runtime))
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).replace(hour=7, minute=5, second=0, microsecond=0)
        self.activity = {"id": 123, "name": "晨跑", "description": "原备注",
                         "start_date": start.isoformat(), "start_date_local": start.replace(tzinfo=None).isoformat() + "Z",
                         "moving_time": 1200, "start_latlng": [39.9, 116.4], "trainer": False, "sport_type": "Run"}
        sample = start.replace(minute=0).timestamp()
        self.scenario = {"activity": self.activity,
                         "weather": {"minutely_15": {
                             "time": [sample, sample + 900, sample + 1800],
                             "temperature_2m": [20, 26, 20], "apparent_temperature": [30, 36, 30],
                             "relative_humidity_2m": [60, 90, 60], "wind_speed_10m": [3, 9, 3],
                             "weather_code": [0, 61, 0], "cloud_cover": [0, 100, 0], "precipitation": [0, 1, 0],
                         }}}
        self.state = self.directory / ".strava_weather_state.json"

    def sync(self, *args, **environment):
        (self.directory / "scenario.json").write_text(json.dumps(self.scenario))
        return subprocess.run(["bash", "update_strava_weather.sh", "sync", *args], cwd=self.directory,
                              env=dict(self.environment, **environment), input="", text=True,
                              capture_output=True, timeout=30)

    def requests(self):
        path = self.directory / "requests.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def clear_requests(self):
        (self.directory / "requests.jsonl").unlink(missing_ok=True)

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


class SyncTests(SyncFixture):
    def test_custom_title_is_used_in_preview_updates_and_repeated_force(self):
        path = self.directory / "config.local.json"
        path.write_text(json.dumps(dict(self.config, title={"template": "{emoji} | {title} · 跑步"})))
        self.assert_success(self.sync("--activity-id", "123", "--dry-run"))
        self.assertFalse(any(item.get("method") == "PUT" for item in self.requests()))
        self.clear_requests()
        self.assert_success(self.sync("--activity-id", "123"))
        body = self.requests()[-1]["body"]
        self.assertEqual(body["name"], "🌧️🥵 | 晨跑 · 跑步")
        self.scenario["activity"].update(body)
        self.clear_requests()
        self.assert_success(self.sync("--activity-id", "123", "--force"))
        self.assertFalse(any(item.get("method") == "PUT" for item in self.requests()))

    def test_title_can_be_disabled_without_affecting_weather(self):
        (self.directory / "config.local.json").write_text(json.dumps(dict(self.config, title={"enabled": False})))
        self.activity["name"] = "晨跑 🏔️"
        self.assert_success(self.sync("--activity-id", "123"))
        self.assertEqual(set(self.requests()[-1]["body"]), {"description", "selected_polyline_style"})

    def test_map_style_prefers_power_and_falls_back_to_pace(self):
        # 默认活动是 Run 且无功率数据：用配速图
        self.assert_success(self.sync("--activity-id", "123"))
        self.assertEqual(self.requests()[-1]["body"]["selected_polyline_style"], "pace")
        # 有功率数据（average_watts）：改用功率图
        self.activity["average_watts"] = 180.5
        self.clear_requests()
        self.assert_success(self.sync("--activity-id", "123", "--force"))
        self.assertEqual(self.requests()[-1]["body"]["selected_polyline_style"], "power")
        # 骑行无功率数据同样回退配速图
        self.activity["sport_type"] = "Ride"
        del self.activity["average_watts"]
        self.clear_requests()
        self.assert_success(self.sync("--activity-id", "123", "--force"))
        self.assertEqual(self.requests()[-1]["body"]["selected_polyline_style"], "pace")

    def test_map_style_absent_for_trail_run_hike_and_when_disabled(self):
        # 越野跑即使有功率数据也不动地图样式
        self.activity["sport_type"] = "TrailRun"
        self.activity["average_watts"] = 200.0
        self.assert_success(self.sync("--activity-id", "123"))
        self.assertNotIn("selected_polyline_style", self.requests()[-1]["body"])
        # 徒步同越野跑一样不动地图样式
        self.activity["sport_type"] = "Hike"
        self.clear_requests()
        self.assert_success(self.sync("--activity-id", "123", "--force"))
        self.assertNotIn("selected_polyline_style", self.requests()[-1]["body"])
        # 配置关闭后普通跑步也不设置
        (self.directory / "config.local.json").write_text(json.dumps(dict(self.config, map_style={"enabled": False})))
        self.activity["sport_type"] = "Run"
        self.clear_requests()
        self.assert_success(self.sync("--activity-id", "123", "--force"))
        self.assertNotIn("selected_polyline_style", self.requests()[-1]["body"])

    def test_gearaut_weather_with_power_gets_power_style_only(self):
        # gearaut 天气在场时不查天气、不动描述；有功率数据只下发功率样式
        self.scenario["activity"] = dict(self.activity, description=BLOCK, average_watts=210.0)
        self.assert_success(self.sync("--activity-id", "123"))
        puts = [entry for entry in self.requests() if entry.get("method") == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0]["body"], {"selected_polyline_style": "power"})
        self.assertFalse(any(entry["kind"] == "weather" for entry in self.requests()))
        self.assertEqual(json.loads(self.state.read_text())["activities"], {"123": "updated"})

    def test_gearaut_weather_trail_run_left_untouched(self):
        # 越野跑不设样式、无山峰可加时，gearaut 天气活动完全不动
        self.scenario["activity"] = dict(self.activity, sport_type="TrailRun",
                                          description=BLOCK, average_watts=200.0)
        self.assert_success(self.sync("--activity-id", "123"))
        self.assertFalse(any(entry.get("method") == "PUT" for entry in self.requests()))
        self.assertEqual(json.loads(self.state.read_text())["activities"], {"123": "existing"})

    def test_short_ride_commute_fix_shares_one_put(self):
        # ≤10km 非通勤骑行：通勤+静音与地图样式同一次 PUT；可见性记入待处理清单
        self.scenario["activity"] = dict(self.activity, sport_type="Ride", distance=8000.0,
                                          commute=False, description=BLOCK)
        self.assert_success(self.sync("--activity-id", "123"))
        puts = [entry for entry in self.requests() if entry.get("method") == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0]["body"], {"selected_polyline_style": "pace",
                                           "commute": True, "hide_from_home": True})
        pending = json.loads((self.directory / ".strava_visibility_pending.json").read_text())
        self.assertEqual(pending, ["123"])

    def test_short_ride_without_weather_gets_everything_in_one_put(self):
        # 无天气的短骑行：天气块、样式、通勤、静音仍只发一次 PUT
        self.scenario["activity"] = dict(self.activity, sport_type="Ride", distance=8000.0,
                                          commute=False)
        self.assert_success(self.sync("--activity-id", "123"))
        puts = [entry for entry in self.requests() if entry.get("method") == "PUT"]
        self.assertEqual(len(puts), 1)
        body = puts[0]["body"]
        self.assertIn("温度:", body["description"])
        self.assertEqual(body["selected_polyline_style"], "pace")
        self.assertEqual(body["commute"], True)
        self.assertEqual(body["hide_from_home"], True)

    def test_long_ride_is_not_commute_fixed(self):
        self.scenario["activity"] = dict(self.activity, sport_type="Ride", distance=15000.0,
                                          commute=False, description=BLOCK)
        self.assert_success(self.sync("--activity-id", "123"))
        puts = [entry for entry in self.requests() if entry.get("method") == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0]["body"], {"selected_polyline_style": "pace"})
        self.assertFalse((self.directory / ".strava_visibility_pending.json").exists())

    def test_already_commute_short_ride_is_not_refixed(self):
        self.scenario["activity"] = dict(self.activity, sport_type="Ride", distance=5000.0,
                                          commute=True, description=BLOCK)
        self.assert_success(self.sync("--activity-id", "123"))
        puts = [entry for entry in self.requests() if entry.get("method") == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0]["body"], {"selected_polyline_style": "pace"})
        self.assertFalse((self.directory / ".strava_visibility_pending.json").exists())

    def test_invalid_title_fails_before_any_requests_or_state_updates(self):
        (self.directory / "config.local.json").write_text(json.dumps(dict(self.config, title={"template": "{wrong}"})))
        result = self.sync("--activity-id", "123")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("标题模板", result.stderr)
        self.assertEqual(self.requests(), [])
        self.assertFalse(self.state.exists())

    def test_inline_template_and_scan_defaults_come_from_shared_config(self):
        (self.directory / "config.local.json").write_text(json.dumps(dict(self.config,
            weather={"template": "天气 {t_min:.1f}～{t_max:.1f}°C"}, sync={"page_size": 17, "lookback_days": 2})))
        self.assert_success(self.sync())
        self.assertEqual(self.requests()[-1]["body"]["description"], "原备注\n天气 22.0～26.0°C")
        query = parse_qs(urlsplit(self.requests()[0]["url"]).query)
        self.assertEqual(query["per_page"], ["17"])
        self.assertAlmostEqual(int(query["before"][0]) - int(query["after"][0]), 2 * 86400, delta=2)

    def test_without_custom_file_environment_credentials_use_default_behavior(self):
        (self.directory / "config.local.json").unlink()
        self.assert_success(self.sync("--activity-id", "123", CLIENT_ID="test-client-id", CLIENT_SECRET="test-client-secret"))
        self.assertIn("Not Powered By www.gearaut.com 🍚", self.requests()[-1]["body"]["description"])

    def test_missing_weather_is_written_once_and_cached_with_private_permissions(self):
        self.activity["description"] += "\nhttps://summitbag.com/example"
        self.assert_success(self.sync())
        requests = self.requests()
        self.assertEqual(len(requests), 5)  # list, detail, streams(山峰匹配), weather, PUT
        body = requests[-1]["body"]
        self.assertEqual(body["name"], "晨跑 🌧️🥵")
        # 旧 summitbag 行被清除，不再保留
        self.assertNotIn("https://summitbag.com/example", body["description"])
        self.assertIn("温度: 22.00°C - 26.00°C", body["description"])
        self.assertIn("体感温度: 32.00°C - 36.00°C", body["description"])
        self.assertIn("湿度: 70.00% - 90.00%", body["description"])
        self.assertIn("风速: 5.00 km/h - 9.00 km/h", body["description"])
        self.assertTrue(body["description"].endswith("Not Powered By www.gearaut.com 🍚"))
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(list(self.runtime.iterdir()), [])
        self.clear_requests()
        self.assert_success(self.sync())
        # 已缓存活动不再为重排重新入队，只查列表
        self.assertEqual(len(self.requests()), 1)
        self.assertIn("/athlete/activities?", self.requests()[0]["url"])
        self.assertFalse(any(entry["kind"] == "weather" or entry.get("method") == "PUT" for entry in self.requests()))

    def test_today_activity_uses_forecast_and_provider_apparent_temperature(self):
        start = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
        self.activity.update(start_date=start.isoformat(), start_date_local=start.replace(tzinfo=None).isoformat() + "Z",
                             moving_time=0)
        sample = start.timestamp()
        self.scenario["weather"]["minutely_15"]["time"] = [sample, sample + 900, sample + 1800]
        self.assert_success(self.sync("--activity-id", "123"))
        weather = next(entry for entry in self.requests() if entry["kind"] == "weather")
        parsed = urlsplit(weather["url"])
        self.assertEqual(parsed.netloc, "api.open-meteo.com")
        self.assertIn("apparent_temperature", parse_qs(parsed.query)["minutely_15"][0].split(","))
        description = self.requests()[-1]["body"]["description"]
        self.assertIn("温度: 20.00°C - 20.00°C  体感温度: 30.00°C - 30.00°C", description)
        self.assertTrue(description.endswith("Not Powered By www.gearaut.com 🍚"))

    def test_own_weather_signature_skips_weather_but_still_applies_map_style(self):
        # 已有我方天气时不查天气，但地图样式仍按流水线在一次 PUT 里下发
        self.scenario["activity"]["description"] = BLOCK.replace(
            "Powered By www.gearaut.com", "Not Powered By www.gearaut.com 🍚")
        self.assert_success(self.sync())
        self.assertEqual(json.loads(self.state.read_text())["activities"], {"123": "updated"})
        self.assertFalse(any(entry["kind"] == "weather" for entry in self.requests()))
        puts = [entry for entry in self.requests() if entry.get("method") == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0]["body"], {"selected_polyline_style": "pace"})

    def test_existing_weather_and_indoor_activities_are_remembered(self):
        for indoor in (False, True):
            with self.subTest(indoor=indoor):
                self.state.unlink(missing_ok=True)
                self.clear_requests()
                self.scenario["activity"] = dict(self.activity, trainer=indoor, description="原备注\n" + BLOCK)
                self.assert_success(self.sync())
                if indoor:
                    self.assertTrue(all(entry.get("method") == "GET" for entry in self.requests()))
                else:
                    # 有天气只跳过天气步；地图样式仍在一次 PUT 里下发
                    puts = [entry for entry in self.requests() if entry.get("method") == "PUT"]
                    self.assertEqual([put["body"] for put in puts], [{"selected_polyline_style": "pace"}])
                self.clear_requests()
                self.assert_success(self.sync())
                # 已缓存活动不再为重排/山峰更新重新入队，只查列表
                self.assertEqual(len(self.requests()), 1)

    def test_dry_run_never_writes_remote_activity_or_processed_state(self):
        for arguments, environment in ((("--dry-run",), {}), ((), {"DRY_RUN": "1", "YES": "1"})):
            with self.subTest(arguments=arguments):
                self.clear_requests()
                self.assert_success(self.sync(*arguments, **environment))
                self.assertTrue(all(entry.get("method") != "PUT" for entry in self.requests()))
                self.assertFalse(self.state.exists())

    def test_force_recalculates_cached_activity_and_existing_weather(self):
        for signature in ("Not Powered By www.gearaut.com 🍚", "Not Powered By www.gearaut.com 👻",
                          "Powered By www.gearaut.com"):
            with self.subTest(signature=signature):
                block = BLOCK.replace("Powered By www.gearaut.com", signature)
                self.scenario["activity"] = dict(self.activity, description="原备注\n" + block)
                self.assert_success(self.sync())
                self.clear_requests()
                self.assert_success(self.sync("--force"))
                self.assertEqual(self.requests()[-1]["method"], "PUT")
                description = self.requests()[-1]["body"]["description"]
                self.assertEqual(description.count("温度:"), 2)
                self.assertEqual(description.count("Not Powered By"), 1)
                self.assertTrue(description.startswith("原备注\n温度: 22.00°C"))
                self.assertTrue(description.endswith("Not Powered By www.gearaut.com 🍚"))

    def test_failures_are_not_cached_and_can_be_retried(self):
        for failure in ({"weather_error": True}, {"put_status": 500}, {"put_id": 999}):
            with self.subTest(failure=failure):
                self.state.unlink(missing_ok=True)
                self.scenario.update(failure)
                self.assertNotEqual(self.sync().returncode, 0)
                state = json.loads(self.state.read_text())
                self.assertNotIn("123", state["activities"])
                self.assertIn("123", state["pending"])
                for key in failure:
                    self.scenario.pop(key)
        self.assert_success(self.sync())
        self.assertTrue(self.state.exists())

    def test_404_activity_is_cleared_from_pending_queue(self):
        # 活动已删除（详情 404）：记为 gone 并从待处理队列移出，同步仍算成功；
        # 否则每轮 sync 的列表扫描都会把它重新入队，反复白耗配额
        self.scenario["get_status_by_id"] = {"123": 404}
        self.assert_success(self.sync())
        state = json.loads(self.state.read_text())
        self.assertNotIn("123", state["pending"])
        self.assertEqual(state["activities"].get("123"), "gone")
        # 第二轮列表扫描不再把它入队，不再请求这条活动
        before = len(self.requests())
        self.assert_success(self.sync())
        self.assertFalse(any("activities/123" in entry["url"] for entry in self.requests()[before:]))

    def test_empty_activity_list_advances_scan_without_completed_records(self):
        self.scenario["empty"] = True
        self.assert_success(self.sync())
        self.assertEqual(len(self.requests()), 1)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["activities"], {})
        self.assertEqual(state["pending"], {})
        self.assertIsInstance(state["last_scan"], int)

    def test_coordinates_use_stream_fallback_and_allow_explicit_location(self):
        self.scenario["activity"] = dict(self.activity, start_latlng=[])
        self.assert_success(self.sync())
        self.assertTrue(any("/streams?" in entry["url"] for entry in self.requests()))
        self.state.unlink()
        self.clear_requests()
        self.scenario["streams_status"] = 404
        self.assertNotEqual(self.sync().returncode, 0)
        self.assertIn("123", json.loads(self.state.read_text())["pending"])
        self.clear_requests()
        self.scenario.pop("streams_status", None)
        # 参与山峰匹配的类型（Run、Ride 等）都会拉轨迹 streams 做匹配；
        # 用不参与的 EBikeRide 隔离验证"显式坐标让天气不再需要轨迹回退"
        self.scenario["activity"] = dict(self.activity, sport_type="EBikeRide")
        self.assert_success(self.sync("--activity-id", "123", "--latitude", "39.9", "--longitude", "116.4"))
        self.assertFalse(any("/streams?" in entry["url"] for entry in self.requests()))
    def test_single_activity_uses_one_detail_read_and_keeps_handwritten_prefix(self):
        prefix = "⛰️ 今天的手写游记\n\n第一段\n第二段\n"
        self.scenario["activity"] = dict(self.activity, description=prefix + MOUNTAINS)
        self.assert_success(self.sync("--activity-id", "123"))
        requests = self.requests()
        # 详情 GET + 山峰匹配的轨迹 streams GET + 写回 PUT
        self.assertEqual([entry["method"] for entry in requests if entry["kind"] == "strava"], ["GET", "GET", "PUT"])
        # summitbag 格式的旧峰行被清掉，手写前缀保留
        self.assertTrue(requests[-1]["body"]["description"].startswith(prefix + "温度:"))
        self.assertEqual(list(self.runtime.iterdir()), [])

    def test_cached_activities_are_not_requeued_for_peak_updates(self):
        # 已缓存活动不再为山峰更新重新入队，直接跳过
        self.write_state(activities={"123": "updated"})
        description = BLOCK + "\n" + MOUNTAINS
        self.scenario["activity"] = dict(self.activity, description=description)
        self.assert_success(self.sync())
        # 无 PUT：缓存活动跳过，不重排山峰
        self.assertFalse(any(entry.get("method") == "PUT" for entry in self.requests()))
        self.clear_requests()
        self.assert_success(self.sync())
        self.assertFalse(any(entry["kind"] == "weather" or entry.get("method") == "PUT" for entry in self.requests()))

    def test_disabled_reorder_keeps_cached_activity_fast_skip(self):
        self.write_state(activities={"123": "updated"})
        self.scenario["activity"] = dict(self.activity, description=BLOCK + "\n" + MOUNTAINS)
        self.assert_success(self.sync("--no-reorder-summitbag"))
        self.assertEqual(len(self.requests()), 1)

    def test_computed_weather_updates_only_description_if_title_is_already_correct(self):
        self.scenario["activity"] = dict(self.activity, name="晨跑 🌧️🥵")
        self.assert_success(self.sync("--activity-id", "123"))
        self.assertEqual(set(self.requests()[-1]["body"]), {"description", "selected_polyline_style"})
        # 详情 GET + 山峰匹配的轨迹 streams GET
        self.assertEqual(sum(entry.get("method") == "GET" for entry in self.requests()), 2)

    def test_peak_update_failure_remains_pending_and_is_retried(self):
        # 山峰更新在正常处理流程中进行，失败进 pending 重试
        # 不预缓存，让活动作为新活动处理（有天气，走山峰更新路径）
        self.scenario["activity"] = dict(self.activity, description=BLOCK + "\n" + MOUNTAINS)
        self.scenario["put_status"] = 500
        self.assertNotEqual(self.sync().returncode, 0)
        self.assertEqual(json.loads(self.state.read_text())["pending"], {"123": False})
        self.scenario.pop("put_status")
        self.clear_requests()
        self.assert_success(self.sync())
        self.assertEqual(self.requests()[-1]["method"], "PUT")
        self.assertFalse(any(entry["kind"] == "weather" for entry in self.requests()))

    def test_invalid_reorder_setting_fails_before_requests(self):
        result = self.sync(REORDER_SUMMITBAG="invalid")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REORDER_SUMMITBAG", result.stderr)
        self.assertEqual(self.requests(), [])

    def test_explicit_activity_skips_list_and_cache_survives_preview(self):
        self.assert_success(self.sync("--activity-id", "123"))
        self.assertFalse(any("/athlete/activities?" in entry["url"] for entry in self.requests()))
        before = self.state.read_bytes()
        self.assert_success(self.sync("--force", "--dry-run", "--activity-id", "123"))
        self.assertEqual(self.state.read_bytes(), before)

    def test_symlinked_state_is_rejected_without_requests(self):
        target = self.directory / "target.json"
        target.write_text('{}')
        self.state.symlink_to(target)
        self.assertNotEqual(self.sync().returncode, 0)
        self.assertEqual(target.read_text(), '{}')
        self.assertEqual(self.requests(), [])

    def test_concurrent_run_skips_before_any_api_call(self):
        with (self.directory / ".strava_weather_state.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_success(self.sync())
        self.assertEqual(self.requests(), [])

    def test_missing_authorization_fails_without_opening_a_browser(self):
        (self.directory / ".strava_tokens.json").unlink()
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("login", result.stderr)
        self.assertEqual(self.requests(), [])

    def test_domain_signature_skips_weather_but_still_applies_map_style(self):
        # gearaut 已写天气：我方兜底不查天气、不改描述；地图样式仍走我方逻辑
        self.scenario["activity"] = dict(self.activity, description="温度: 20°C\nPowered By www.gearaut.com")
        self.assert_success(self.sync())
        self.assertFalse(any(entry["kind"] == "weather" for entry in self.requests()))
        puts = [entry for entry in self.requests() if entry.get("method") == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0]["body"], {"selected_polyline_style": "pace"})

    def test_custom_template_is_selected_from_config_or_environment_and_cli_takes_priority(self):
        self.activity["description"] += "\nhttps://summitbag.com/example"
        (self.directory / "custom.txt").write_text("我的天气 {emoji}\n气温 {t_min:.1f}～{t_max:.1f}°C\n风速 {w_max:.0f} km/h\n")
        config = self.directory / "config.local.json"
        for source in ("config", "environment", "cli"):
            with self.subTest(source=source):
                self.state.unlink(missing_ok=True)
                self.clear_requests()
                configured = "custom.txt" if source == "config" else "missing.txt"
                config.write_text(json.dumps(dict(self.config, weather={"template_file": configured})))
                arguments = ("--template", "custom.txt") if source == "cli" else ()
                environment = {"WEATHER_TEMPLATE_FILE": "missing.txt" if source == "cli" else "custom.txt"} if source != "config" else {}
                self.assert_success(self.sync("--activity-id", "123", *arguments, **environment))
                self.assertEqual(self.requests()[-1]["body"]["description"],
                                 "原备注\n我的天气 🌧️🥵\n气温 22.0～26.0°C\n风速 9 km/h")

    def test_custom_weather_is_detected_from_template_even_with_different_values(self):
        (self.directory / "custom.txt").write_text("气温 {t_min:.1f}～{t_max:.1f}°C")
        self.scenario["activity"] = dict(self.activity, description="原备注\n气温 -2.0～5.0°C\n后记")
        self.assert_success(self.sync("--template", "custom.txt"))
        # 自定义模板天气被识别：不查天气；地图样式仍在一次 PUT 里下发
        self.assertFalse(any(entry["kind"] == "weather" for entry in self.requests()))
        puts = [entry for entry in self.requests() if entry.get("method") == "PUT"]
        self.assertEqual([put["body"] for put in puts], [{"selected_polyline_style": "pace"}])

    def test_force_replaces_custom_weather_and_preserves_surrounding_notes(self):
        (self.directory / "custom.txt").write_text("我的天气 {emoji}\n气温 {t_min:.1f}～{t_max:.1f}°C")
        self.scenario["activity"] = dict(self.activity, description="原备注\n我的天气 🌙\n气温 -2.0～5.0°C\n后记\nhttps://summitbag.com/example")
        self.assert_success(self.sync("--template", "custom.txt", "--force"))
        self.assertEqual(self.requests()[-1]["body"]["description"],
                         "原备注\n后记\n我的天气 🌧️🥵\n气温 22.0～26.0°C")

    def test_forced_custom_weather_with_identical_values_is_a_noop(self):
        (self.directory / "custom.txt").write_text("气温 {t_min:.1f}～{t_max:.1f}°C")
        self.scenario["activity"] = dict(self.activity, name="晨跑 🌧️🥵", description="手写备注\n气温 22.0～26.0°C")
        self.assert_success(self.sync("--template", "custom.txt", "--force"))
        self.assertTrue(any(entry["kind"] == "weather" for entry in self.requests()))
        self.assertFalse(any(entry.get("method") == "PUT" for entry in self.requests()))

    def test_template_errors_fail_before_requests_or_state_changes(self):
        for source in (None, "气温 {unknown}", "气温 {t_min:{t_max}}", ""):
            with self.subTest(source=source):
                path = self.directory / "bad.txt"
                if source is not None:
                    path.write_text(source)
                result = self.sync("--template", "bad.txt")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("模板", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(self.requests(), [])
                self.assertFalse(self.state.exists())

    def test_force_does_not_append_to_an_unlocatable_domain_block(self):
        self.scenario["activity"] = dict(self.activity, description="原备注\n旧布局 www.gearaut.com\n后记")
        result = self.sync("--force")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("无法定位", result.stderr)
        self.assertFalse(any(entry.get("method") == "PUT" for entry in self.requests()))

    def test_empty_template_argument_fails_before_requests_or_state_changes(self):
        result = self.sync("--template", "")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("模板", result.stderr)
        self.assertEqual(self.requests(), [])
        self.assertFalse(self.state.exists())

    def test_title_emoji_alone_does_not_prevent_description_from_being_added(self):
        self.scenario["activity"] = dict(self.activity, name="晨跑 🌞")
        self.assert_success(self.sync())
        self.assertEqual(self.requests()[-1]["method"], "PUT")

    def test_invalid_weather_response_has_a_clear_error_and_remains_retryable(self):
        self.scenario["weather"] = {"minutely_15": None}
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("123", json.loads(self.state.read_text())["pending"])

    def test_invalid_arguments_fail_before_any_api_call(self):
        for arguments in (("--activity-id", ""), ("--activity-id", "abc"),
                          ("--latitude", "91", "--longitude", "0"), ("--latitude", "39.9")):
            with self.subTest(arguments=arguments):
                self.clear_requests()
                self.assertNotEqual(self.sync(*arguments).returncode, 0)
                self.assertEqual(self.requests(), [])

    def test_invalid_state_is_reported_without_replacing_records(self):
        for content in ('{}', '{"version":2,"activities":{"123":"updated"}}', 'not json'):
            with self.subTest(content=content):
                self.clear_requests()
                self.state.write_text(content)
                self.assertNotEqual(self.sync().returncode, 0)
                self.assertEqual(self.state.read_text(), content)
                self.assertEqual(self.requests(), [])

    def batch(self, *ids):
        activities = [dict(self.activity, id=activity_id, description=BLOCK) for activity_id in ids]
        self.scenario["activities"] = activities
        self.scenario["details"] = {str(item["id"]): item for item in activities}
        return activities

    def write_state(self, activities=None, pending=None, last_scan=None):
        self.state.write_text(json.dumps({"version": 2, "activities": activities or {},
                                         "pending": pending or {}, "last_scan": last_scan}))

    def test_batch_pages_past_cached_activity_and_deduplicates_ids(self):
        first, second, third = self.batch(123, 124, 125)
        self.scenario["pages"] = [[first, second], [second, third], []]
        self.state.write_text(json.dumps({"version": 1, "activities": {"123": "updated"}}))
        self.assert_success(self.sync("--page-size", "2"))
        state = json.loads(self.state.read_text())
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["activities"], {"123": "updated", "124": "updated", "125": "updated"})
        details = [entry for entry in self.requests()
                   if entry["method"] == "GET" and "/activities/" in entry["url"] and "/streams?" not in entry["url"]]
        # 123 已缓存，不再拉取详情
        self.assertEqual([entry["url"].rsplit("/", 1)[-1] for entry in details], ["124", "125"])
        pages = [parse_qs(urlsplit(entry["url"]).query) for entry in self.requests() if "/athlete/activities?" in entry["url"]]
        self.assertEqual([page["page"][0] for page in pages], ["1", "2", "3"])
        self.assertEqual(len({page["before"][0] for page in pages}), 1)

    def test_failure_is_retried_outside_next_window_while_new_activity_completes(self):
        self.batch(123, 124, 125)
        self.scenario["get_status_by_id"] = {"124": 500}
        self.assertNotEqual(self.sync().returncode, 0)
        state = json.loads(self.state.read_text())
        self.assertEqual(set(state["activities"]), {"123", "125"})
        self.assertEqual(state["pending"], {"124": False})
        self.scenario["get_status_by_id"] = {}
        self.scenario["activities"] = [dict(self.activity, id=126, description=BLOCK)]
        self.clear_requests()
        self.assert_success(self.sync())
        state = json.loads(self.state.read_text())
        self.assertEqual(set(state["activities"]), {"123", "124", "125", "126"})
        self.assertEqual(state["pending"], {})

    def test_scan_failure_preserves_discovered_queue_and_previous_scan_time(self):
        self.batch(123, 124, 125)
        previous = int(dt.datetime.now(dt.timezone.utc).timestamp()) - 86400
        self.write_state(last_scan=previous)
        self.scenario["list_status_by_page"] = {"2": 500}
        self.assertNotEqual(self.sync("--page-size", "2").returncode, 0)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["last_scan"], previous)
        self.assertEqual(set(state["pending"]), {"123", "124"})
        self.assertEqual(state["activities"], {})
        self.scenario["list_status_by_page"] = {}
        self.assert_success(self.sync("--page-size", "2"))
        state = json.loads(self.state.read_text())
        self.assertGreater(state["last_scan"], previous)
        self.assertEqual(set(state["activities"]), {"123", "124", "125"})

    def test_default_query_covers_downtime_and_overlaps_previous_scan(self):
        self.batch(123)
        previous = int(dt.datetime.now(dt.timezone.utc).timestamp()) - 30 * 86400
        self.write_state(last_scan=previous)
        self.assert_success(self.sync())
        query = parse_qs(urlsplit(self.requests()[0]["url"]).query)
        self.assertEqual(int(query["after"][0]), previous - 7 * 86400 - 1)

    def test_date_backfill_is_inclusive_and_does_not_advance_default_scan(self):
        activities = self.batch(123, 124, 125)
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=20)).replace(hour=0, minute=0, second=0, microsecond=0)
        for item, offset in zip(activities, (0, 2 * 86400 - 1, 2 * 86400)):
            item["start_date"] = (start + dt.timedelta(seconds=offset)).isoformat()
        previous = int(dt.datetime.now(dt.timezone.utc).timestamp()) - 86400
        self.write_state(pending={"999": False}, last_scan=previous)
        self.assert_success(self.sync("--since", start.date().isoformat(), "--until", (start + dt.timedelta(days=1)).date().isoformat()))
        state = json.loads(self.state.read_text())
        self.assertEqual(state["last_scan"], previous)
        self.assertEqual(set(state["activities"]), {"123", "124"})
        self.assertEqual(state["pending"], {"999": False})

    def test_all_history_finds_old_uploads_outside_default_window(self):
        activities = self.batch(123, 124)
        activities[1]["start_date"] = "2010-01-01T00:00:00+00:00"
        self.assert_success(self.sync())
        self.assertEqual(set(json.loads(self.state.read_text())["activities"]), {"123"})
        self.assert_success(self.sync("--all"))
        self.assertEqual(set(json.loads(self.state.read_text())["activities"]), {"123", "124"})

    def test_rate_limit_stops_batch_without_losing_unattempted_ids(self):
        self.batch(123, 124, 125)
        self.scenario["get_status_by_id"] = {"123": 429}
        self.assertNotEqual(self.sync().returncode, 0)
        state = json.loads(self.state.read_text())
        self.assertEqual(set(state["pending"]), {"123", "124", "125"})
        self.assertEqual(state["activities"], {})
        self.assertEqual(len(self.requests()), 2)

    def test_forced_failure_retries_with_force_on_next_normal_run(self):
        self.batch(123)
        self.write_state(activities={"123": "existing"})
        self.scenario["put_status"] = 500
        self.assertNotEqual(self.sync("--force").returncode, 0)
        self.assertEqual(json.loads(self.state.read_text())["pending"], {"123": True})
        self.scenario.pop("put_status")
        self.clear_requests()
        self.assert_success(self.sync())
        self.assertEqual(self.requests()[-1]["method"], "PUT")
        self.assertEqual(json.loads(self.state.read_text())["pending"], {})

    def test_batch_preview_preserves_scan_progress_and_queue(self):
        self.batch(123, 124, 125)
        self.write_state(activities={"123": "updated"}, pending={"124": False}, last_scan=10)
        before = self.state.read_bytes()
        self.assert_success(self.sync("--dry-run"))
        self.assertEqual(self.state.read_bytes(), before)
        self.assertTrue(all(entry.get("method") != "PUT" for entry in self.requests()))

    def test_invalid_batch_arguments_make_no_requests(self):
        for arguments in (("--all", "--since", "2020-01-01"), ("--until", "2020-01-01"),
                          ("--since", "invalid"), ("--since", "2020-02-01", "--until", "2020-01-01"),
                          ("--activity-id", "123", "--all"), ("--page-size", "0"), ("--page-size", "201"),
                          ("--latitude", "39.9", "--longitude", "116.4")):
            with self.subTest(arguments=arguments):
                self.assertNotEqual(self.sync(*arguments).returncode, 0)
                self.assertEqual(self.requests(), [])

    def test_interrupted_initial_scan_keeps_original_query_start(self):
        activities = self.batch(123)
        now = int(dt.datetime.now(dt.timezone.utc).timestamp())
        activities[0]["start_date"] = dt.datetime.fromtimestamp(now - 20 * 86400, dt.timezone.utc).isoformat()
        self.write_state()
        state = json.loads(self.state.read_text())
        state["scan_from"] = now - 30 * 86400
        self.state.write_text(json.dumps(state))
        self.assert_success(self.sync())
        query = parse_qs(urlsplit(self.requests()[0]["url"]).query)
        self.assertEqual(int(query["after"][0]), state["scan_from"] - 1)
        self.assertEqual(set(json.loads(self.state.read_text())["activities"]), {"123"})


class WeatherMathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="strava-math-")
        self.addCleanup(temporary.cleanup)
        self.config_root = Path(temporary.name)
        shutil.copy2(ROOT / "config.default.json", self.config_root)
        config_root = patch("weather_template.ROOT", self.config_root)
        config_root.start()
        self.addCleanup(config_root.stop)

    def load_module(self):
        spec = importlib.util.spec_from_file_location("strava_sync", ROOT / "strava_sync.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.ROOT = self.config_root
        return module

    def test_activity_snapshot_handoff_is_private_and_cleaned_up(self):
        module = self.load_module()
        args = module.argparse.Namespace(dry_run=False, reorder_summitbag=True, template=None)
        activity = {"id": 123, "name": "晨跑", "description": "手写备注"}
        snapshots = []

        def invoke(command, activity_id=None, extra=None, arguments=()):
            self.assertEqual(activity_id, "123")
            path = Path(extra["STRAVA_SYNC_ACTIVITY_FILE"])
            snapshots.append(path)
            self.assertEqual(json.loads(path.read_text()), activity)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        with patch.object(module, "invoke", side_effect=invoke):
            module.update_activity(args, activity, description="天气说明")
        self.assertTrue(snapshots)
        self.assertFalse(snapshots[0].parent.exists())

    def test_current_and_legacy_weather_signatures_are_recognized(self):
        module = self.load_module()
        for signature in ("Not Powered By www.gearaut.com 🍚", "Not Powered By www.gearaut.com 👻",
                          "Powered By www.gearaut.com"):
            for newline in ("\n", "\r\n"):
                with self.subTest(signature=signature, newline=newline):
                    block = BLOCK.replace("Powered By www.gearaut.com", signature).replace("\n", newline)
                    self.assertTrue(module.has_weather("原备注" + newline + block))

    def test_boundaries_interior_extremes_and_invalid_data(self):
        module = self.load_module()
        self.assertEqual(module.interval_extrema([0, 900, 1800], [20, 26, 20], 300, 1500), (22, 26))
        self.assertEqual(module.interval_extrema([0, 900], [-5, -2], 0, 0), (-5, -5))
        for values, start, end in (([None, 26, 20], 300, 1500), ([20, 26, 20], -1, 900), (None, 0, 900)):
            with self.assertRaises(module.SyncError):
                module.interval_extrema([0, 900, 1800], values, start, end)

    def test_forecast_archive_and_cross_day_query_ranges(self):
        module = self.load_module()
        now = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
        start = dt.datetime(2026, 10, 13, 23, 55, tzinfo=dt.timezone.utc).timestamp()
        url, key = module.weather_url(39.9, 116.4, start, start + 1200, now=now)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(key, "minutely_15")
        self.assertIn("api.open-meteo.com", url)
        self.assertEqual(query["timezone"], ["GMT"])
        self.assertEqual(query["timeformat"], ["unixtime"])
        self.assertEqual(query["end_minutely_15"], ["2026-10-14T00:15"])
        url, key = module.weather_url(39.9, 116.4, start - 864000, start - 864000 + 1200, now=now)
        self.assertEqual(key, "hourly")
        self.assertIn("archive-api.open-meteo.com", url)

    def test_emoji_rules_and_local_clock_are_deterministic(self):
        module = self.load_module()
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).replace(hour=0, minute=0, second=0, microsecond=0)
        activity = {"start_date": start.isoformat(), "start_date_local": start.strftime("%Y-%m-%dT08:00:00Z"), "moving_time": 900}
        for code, emoji in ((0, "🌞"), (2, "⛅"), (3, "☁️"), (45, "🌫️"), (61, "🌧️"), (71, "❄️"), (95, "⛈️")):
            series = {"time": [start.timestamp(), start.timestamp() + 900],
                      "temperature_2m": [20, 20], "apparent_temperature": [20, 20],
                      "relative_humidity_2m": [60, 60], "wind_speed_10m": [3, 3], "weather_code": [code, code]}
            with patch.object(module.urllib.request, "urlopen", return_value=io.StringIO(json.dumps({"hourly": series}))):
                self.assertEqual(module.weather_annotation(activity, (39.9, 116.4))[0], emoji)
        series.update(weather_code=[0, 0], apparent_temperature=[-2, -1], wind_speed_10m=[31, 31])
        activity["start_date_local"] = start.strftime("%Y-%m-%dT00:00:00Z")
        with patch.object(module.urllib.request, "urlopen", return_value=io.StringIO(json.dumps({"hourly": series}))):
            self.assertEqual(module.weather_annotation(activity, (39.9, 116.4))[0], "🌙💨🥶")

    def test_completed_records_are_saved_before_next_activity_starts(self):
        module = self.load_module()
        with tempfile.TemporaryDirectory(prefix="strava-state-") as directory:
            root = Path(directory)
            shutil.copy2(ROOT / "config.default.json", root)
            activities = [{"id": 123}, {"id": 124}]
            calls = []

            def process(args, activity_id, completed, force):
                durable = json.loads((root / ".strava_weather_state.json").read_text())
                calls.append(activity_id)
                if activity_id == "123":
                    self.assertEqual(set(durable["pending"]), {"123", "124"})
                    self.assertIsInstance(durable["last_scan"], int)
                    return "existing"
                self.assertEqual(durable["activities"], {"123": "existing"})
                self.assertEqual(durable["pending"], {"124": False})
                raise KeyboardInterrupt  # Simulate interruption before the next item finishes.

            with patch.object(module, "ROOT", root), patch.object(module, "read_strava", return_value=activities), \
                    patch.object(module, "process_activity", side_effect=process), patch.object(module.sys, "argv", ["sync"]):
                with self.assertRaises(KeyboardInterrupt):
                    module.main()
            self.assertEqual(calls, ["123", "124"])
            self.assertEqual(json.loads((root / ".strava_weather_state.json").read_text())["pending"], {"124": False})

    def test_migration_and_save_keep_more_than_128_completed_records(self):
        module = self.load_module()
        with tempfile.TemporaryDirectory(prefix="strava-state-") as directory:
            path = Path(directory) / "state.json"
            activities = {str(value): "updated" for value in range(1, 141)}
            path.write_text(json.dumps({"version": 1, "activities": activities}))
            state = module.load_state(path)
            module.save_state(path, state)
            self.assertEqual(module.load_state(path)["activities"], activities)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
