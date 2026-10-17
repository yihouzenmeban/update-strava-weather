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
previous = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
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
    count = sum(entry.get("kind") == "strava" and entry.get("method") == "GET"
                and entry["url"].endswith("/activities/" + activity_id) for entry in previous)
    activity = details[activity_id]
    fresh = scenario.get("fresh_by_id", {}).get(activity_id, scenario.get("fresh", activity)
            if activity_id == str(scenario["activity"]["id"]) else activity)
    payload = fresh if count else activity
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


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="strava-sync-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        for name in ("update_strava_weather.sh", "strava_common.sh", "strava_sync.py", "weather_template.py"):
            if (ROOT / name).exists():
                shutil.copy2(ROOT / name, self.directory)
        if (ROOT / "templates").exists():
            shutil.copytree(ROOT / "templates", self.directory / "templates")
        (self.directory / "strava_config.sh").write_text('CLIENT_ID="test-client-id"\nCLIENT_SECRET="test-client-secret"\n')
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
                     "WEATHER_EMOJI", "WEATHER_EMOJI_FILE", "WEATHER_TEMPLATE_FILE", "REORDER_SUMMITBAG", "CLIENT_ID", "CLIENT_SECRET"):
            self.environment.pop(name, None)
        self.environment.update(PATH=f'{self.directory / "bin"}:{os.environ["PATH"]}',
                                PYTHONPATH=str(self.directory), TMPDIR=str(self.runtime))
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).replace(hour=7, minute=5, second=0, microsecond=0)
        self.activity = {"id": 123, "name": "晨跑", "description": "原备注",
                         "start_date": start.isoformat(), "start_date_local": start.replace(tzinfo=None).isoformat() + "Z",
                         "moving_time": 1200, "start_latlng": [39.9, 116.4], "trainer": False, "sport_type": "Run"}
        sample = start.replace(minute=0).timestamp()
        self.scenario = {"activity": self.activity, "fresh": dict(self.activity, description="原备注\nhttps://summitbag.com/example"),
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

    def test_missing_weather_is_written_once_and_cached_with_private_permissions(self):
        self.assert_success(self.sync())
        requests = self.requests()
        self.assertEqual(len(requests), 5)  # list, detail, weather, fresh detail, PUT
        body = requests[-1]["body"]
        self.assertEqual(body["name"], "晨跑 🌧️🥵")
        self.assertIn("https://summitbag.com/example", body["description"])
        self.assertIn("温度: 22.00°C - 26.00°C", body["description"])
        self.assertIn("体感温度: 32.00°C - 36.00°C", body["description"])
        self.assertIn("湿度: 70.00% - 90.00%", body["description"])
        self.assertIn("风速: 5.00 km/h - 9.00 km/h", body["description"])
        self.assertTrue(body["description"].endswith("Not Powered By www.gearaut.com 🍚"))
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(list(self.runtime.iterdir()), [])
        self.clear_requests()
        self.assert_success(self.sync())
        self.assertEqual(len(self.requests()), 2)
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

    def test_current_weather_signature_is_skipped_without_weather_requests(self):
        self.scenario["activity"]["description"] = BLOCK.replace(
            "Powered By www.gearaut.com", "Not Powered By www.gearaut.com 🍚")
        self.assert_success(self.sync())
        self.assertEqual(json.loads(self.state.read_text())["activities"], {"123": "existing"})
        self.assertTrue(all(entry["kind"] == "strava" and entry["method"] == "GET" for entry in self.requests()))

    def test_existing_weather_and_indoor_activities_are_remembered(self):
        for indoor in (False, True):
            with self.subTest(indoor=indoor):
                self.state.unlink(missing_ok=True)
                self.clear_requests()
                self.scenario["activity"] = dict(self.activity, trainer=indoor, description="原备注\n" + BLOCK)
                self.assert_success(self.sync())
                self.assertTrue(all(entry.get("method") == "GET" for entry in self.requests()))
                self.clear_requests()
                self.assert_success(self.sync())
                self.assertEqual(len(self.requests()), 1 if indoor else 2)

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
                self.scenario["activity"] = dict(self.activity, description=block)
                self.scenario["fresh"] = dict(self.activity, description="原备注\n" + block)
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
        self.assert_success(self.sync("--activity-id", "123", "--latitude", "39.9", "--longitude", "116.4"))
        self.assertFalse(any("/streams?" in entry["url"] for entry in self.requests()))

    def test_weather_added_during_processing_is_preserved(self):
        self.scenario["fresh"] = dict(self.activity, description="新备注\n" + BLOCK)
        self.assert_success(self.sync())
        self.assertTrue(all(entry.get("method") != "PUT" for entry in self.requests()))
        self.assertTrue(self.state.exists())

    def test_existing_weather_gets_mountains_reordered_by_default_without_weather_request(self):
        self.scenario["activity"] = dict(self.activity, description=BLOCK + "\n\n" + MOUNTAINS)
        self.scenario["fresh"] = dict(self.activity, name="晨跑 🌞", description="最新备注\n" + BLOCK + "\n\n" + MOUNTAINS + "\n后记")
        self.assert_success(self.sync("--activity-id", "123"))
        self.assertEqual(self.requests()[-1]["body"],
                         {"name": "晨跑 🌞", "description": "最新备注\n" + MOUNTAINS + "\n" + BLOCK + "\n\n后记"})
        self.assertFalse(any(entry["kind"] == "weather" for entry in self.requests()))

    def test_later_mountains_are_found_for_cached_activities_and_repeated_sync_does_not_write(self):
        self.write_state(activities={"123": "updated"})
        description = BLOCK + "\n" + MOUNTAINS
        self.scenario["activity"] = dict(self.activity, description=description)
        self.scenario["fresh"] = self.scenario["activity"]
        self.assert_success(self.sync())
        reordered = self.requests()[-1]["body"]["description"]
        self.assertEqual(reordered, MOUNTAINS + "\n" + BLOCK)
        self.scenario["activity"]["description"] = reordered
        self.clear_requests()
        self.assert_success(self.sync())
        self.assertFalse(any(entry["kind"] == "weather" or entry.get("method") == "PUT" for entry in self.requests()))

    def test_reorder_disable_from_config_environment_or_cli_and_enable_override(self):
        config = self.directory / "strava_config.sh"
        original = config.read_text()
        self.scenario["activity"] = dict(self.activity, description=BLOCK + "\n" + MOUNTAINS)
        self.scenario["fresh"] = self.scenario["activity"]
        cases = (("0", {}, (), False), ("1", {"REORDER_SUMMITBAG": "0"}, (), False),
                 ("1", {}, ("--no-reorder-summitbag",), False),
                 ("0", {}, ("--reorder-summitbag",), True))
        for setting, environment, arguments, changed in cases:
            with self.subTest(setting=setting, environment=environment, arguments=arguments):
                config.write_text(original + f'REORDER_SUMMITBAG="{setting}"\n')
                self.state.unlink(missing_ok=True)
                self.clear_requests()
                self.assert_success(self.sync(*arguments, **environment))
                self.assertEqual(any(entry.get("method") == "PUT" for entry in self.requests()), changed)

    def test_disabled_reorder_keeps_cached_activity_fast_skip(self):
        self.write_state(activities={"123": "updated"})
        self.scenario["activity"] = dict(self.activity, description=BLOCK + "\n" + MOUNTAINS)
        self.assert_success(self.sync("--no-reorder-summitbag"))
        self.assertEqual(len(self.requests()), 1)

    def test_reorder_preview_never_writes_remote_or_state(self):
        self.scenario["activity"] = dict(self.activity, description=BLOCK + "\n" + MOUNTAINS)
        self.scenario["fresh"] = self.scenario["activity"]
        result = self.sync("--dry-run")
        self.assert_success(result)
        self.assertIn(MOUNTAINS + "\n" + BLOCK, result.stdout)
        self.assertFalse(any(entry["kind"] == "weather" or entry.get("method") == "PUT" for entry in self.requests()))
        self.assertFalse(self.state.exists())

    def test_weather_arriving_during_calculation_is_only_reordered(self):
        self.scenario["fresh"] = dict(self.activity, name="保留最新标题 🌙", description=BLOCK + "\n" + MOUNTAINS)
        self.assert_success(self.sync())
        self.assertEqual(self.requests()[-1]["body"], {"name": "保留最新标题 🌙", "description": MOUNTAINS + "\n" + BLOCK})

    def test_reorder_failure_is_retried_for_cached_activity(self):
        self.write_state(activities={"123": "updated"})
        self.scenario["activity"] = dict(self.activity, description=BLOCK + "\n" + MOUNTAINS)
        self.scenario["fresh"] = self.scenario["activity"]
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

    def test_custom_weather_and_mountains_are_reordered_without_gps_or_weather(self):
        (self.directory / "custom.txt").write_text("我的天气\n气温 {t_min:.1f}～{t_max:.1f}°C")
        weather = "我的天气\n气温 -2.0～5.0°C"
        self.scenario["activity"] = dict(self.activity, start_latlng=[], description=weather + "\n" + MOUNTAINS)
        self.scenario["fresh"] = self.scenario["activity"]
        self.assert_success(self.sync("--template", "custom.txt"))
        self.assertEqual(self.requests()[-1]["body"]["description"], MOUNTAINS + "\n" + weather)
        self.assertFalse(any(entry["kind"] == "weather" or "/streams?" in entry["url"] for entry in self.requests()))

    def test_manual_update_applies_default_ordering_and_respects_disable(self):
        self.scenario["activity"]["description"] = "原备注"
        for setting in ("1", "0"):
            with self.subTest(setting=setting):
                self.clear_requests()
                (self.directory / "scenario.json").write_text(json.dumps(self.scenario))
                result = subprocess.run(["bash", "update_strava_weather.sh"], cwd=self.directory,
                                        env=dict(self.environment, ACTIVITY_ID="123", YES="1", REORDER_SUMMITBAG=setting,
                                                 WEATHER_EMOJI="🌞", WEATHER_DESC=BLOCK + "\n" + MOUNTAINS),
                                        input="", text=True, capture_output=True, timeout=30)
                self.assert_success(result)
                suffix = MOUNTAINS + "\n" + BLOCK if setting == "1" else BLOCK + "\n" + MOUNTAINS
                self.assertEqual(self.requests()[-1]["body"]["description"], "原备注\n" + suffix)

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

    def test_domain_signature_is_sufficient_to_skip_existing_weather(self):
        self.scenario["activity"] = dict(self.activity, description="温度: 20°C\nPowered By www.gearaut.com")
        self.assert_success(self.sync())
        self.assertFalse(any(entry["kind"] == "weather" or entry.get("method") == "PUT" for entry in self.requests()))

    def test_custom_template_is_selected_from_config_or_environment_and_cli_takes_priority(self):
        (self.directory / "custom.txt").write_text("我的天气 {emoji}\n气温 {t_min:.1f}～{t_max:.1f}°C\n风速 {w_max:.0f} km/h\n")
        config = self.directory / "strava_config.sh"
        original = config.read_text()
        for source in ("config", "environment", "cli"):
            with self.subTest(source=source):
                self.state.unlink(missing_ok=True)
                self.clear_requests()
                configured = "custom.txt" if source == "config" else "missing.txt"
                config.write_text(original + f'WEATHER_TEMPLATE_FILE="{configured}"\n')
                arguments = ("--template", "custom.txt") if source == "cli" else ()
                environment = {"WEATHER_TEMPLATE_FILE": "missing.txt" if source == "cli" else "custom.txt"} if source != "config" else {}
                self.assert_success(self.sync("--activity-id", "123", *arguments, **environment))
                self.assertEqual(self.requests()[-1]["body"]["description"],
                                 "原备注\nhttps://summitbag.com/example\n我的天气 🌧️🥵\n气温 22.0～26.0°C\n风速 9 km/h")

    def test_custom_weather_is_detected_from_template_even_with_different_values(self):
        (self.directory / "custom.txt").write_text("气温 {t_min:.1f}～{t_max:.1f}°C")
        self.scenario["activity"] = dict(self.activity, description="原备注\n气温 -2.0～5.0°C\n后记")
        self.assert_success(self.sync("--template", "custom.txt"))
        self.assertFalse(any(entry["kind"] == "weather" or entry.get("method") == "PUT" for entry in self.requests()))

    def test_force_replaces_custom_weather_and_preserves_surrounding_notes(self):
        (self.directory / "custom.txt").write_text("我的天气 {emoji}\n气温 {t_min:.1f}～{t_max:.1f}°C")
        self.scenario["fresh"] = dict(self.activity, description="原备注\n我的天气 🌙\n气温 -2.0～5.0°C\n后记\nhttps://summitbag.com/example")
        self.assert_success(self.sync("--template", "custom.txt", "--force"))
        self.assertEqual(self.requests()[-1]["body"]["description"],
                         "原备注\n后记\nhttps://summitbag.com/example\n我的天气 🌧️🥵\n气温 22.0～26.0°C")

    def test_fresh_custom_weather_added_during_calculation_prevents_a_write(self):
        (self.directory / "custom.txt").write_text("气温 {t_min:.1f}～{t_max:.1f}°C")
        self.scenario["fresh"] = dict(self.activity, description="新备注\n气温 -2.0～5.0°C")
        self.assert_success(self.sync("--template", "custom.txt"))
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
        self.scenario["fresh"] = dict(self.activity, description="原备注\n旧布局 www.gearaut.com\n后记")
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
        self.scenario["fresh_by_id"] = self.scenario["details"].copy()
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
        self.assertEqual(state["activities"], {"123": "updated", "124": "existing", "125": "existing"})
        details = [entry for entry in self.requests() if "/activities/" in entry["url"]]
        self.assertEqual([entry["url"].rsplit("/", 1)[-1] for entry in details], ["123", "124", "125"])
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
    def load_module(self):
        spec = importlib.util.spec_from_file_location("strava_sync", ROOT / "strava_sync.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

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
