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
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
BLOCK = ("温度: 20.00°C - 22.00°C  体感温度: 30.00°C - 32.00°C\n"
         "湿度: 60.00% - 70.00%  风速: 3.00 km/h - 5.00 km/h\nPowered By www.gearaut.com")

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
    assert url.startswith(("https://api.open-meteo.com/", "https://archive-api.open-meteo.com/",
                           "https://test.qweatherapi.com/")), url
    with open("requests.jsonl", "a") as log:
        log.write(json.dumps({"kind": "weather", "url": url}) + "\n")
    scenario = json.loads(Path("scenario.json").read_text())
    if scenario.get("weather_error"):
        raise urllib.error.URLError("synthetic network failure")
    if "qweatherapi.com/" in url:
        assert request.get_header("X-qw-api-key") == "test-weather-key"
        assert "test-weather-key" not in url
        if "/geo/" in url:
            return Response({"code": "200", "location": [{"id": "101010100", "tz": "Asia/Shanghai"}]})
        return Response(scenario["qweather"])
    return Response(scenario["weather"])
urllib.request.urlopen = urlopen
'''


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="strava-sync-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        for name in ("update_strava_weather.sh", "strava_common.sh", "strava_sync.py"):
            if (ROOT / name).exists():
                shutil.copy2(ROOT / name, self.directory)
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
                     "WEATHER_EMOJI", "WEATHER_EMOJI_FILE", "CLIENT_ID", "CLIENT_SECRET",
                     "WEATHER_PROVIDER", "QWEATHER_API_HOST", "QWEATHER_API_KEY", "QWEATHER_JWT"):
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
        self.assertEqual(len(self.requests()), 1)
        self.assertIn("/athlete/activities?", self.requests()[0]["url"])

    def configure_qweather(self):
        with (self.directory / "strava_config.sh").open("a") as config:
            config.write('WEATHER_PROVIDER="qweather"\nQWEATHER_API_HOST="test.qweatherapi.com"\n'
                         'QWEATHER_API_KEY="test-weather-key"\n')
        start = dt.datetime.fromisoformat(self.activity["start_date"])
        local = start.astimezone(dt.timezone(dt.timedelta(hours=8)))
        self.scenario["qweather"] = {"code": "200", "weatherHourly": [
            {"time": local.replace(minute=0).strftime("%Y-%m-%d %H:%M"),
             "temp": "20", "humidity": "60", "windSpeed": "3", "icon": "100"},
            {"time": (local.replace(minute=0) + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),
             "temp": "26", "humidity": "90", "windSpeed": "9", "icon": "305"},
        ]}

    def test_qweather_config_drives_sync_and_replaces_previous_provider_block(self):
        self.configure_qweather()
        self.scenario["fresh"]["description"] += "\n" + BLOCK
        self.assert_success(self.sync("--activity-id", "123", "--force"))
        requests = self.requests()
        weather = [entry for entry in requests if entry["kind"] == "weather"]
        self.assertEqual(len(weather), 2)
        query = parse_qs(urlsplit(weather[0]["url"]).query)
        self.assertEqual(query["location"], ["116.40,39.90"])
        query = parse_qs(urlsplit(weather[1]["url"]).query)
        self.assertEqual(query["location"], ["101010100"])
        self.assertEqual(query["unit"], ["m"])
        body = requests[-1]["body"]
        self.assertEqual(body["name"], "晨跑 🌞")
        self.assertIn("温度: 20.50°C - 22.50°C", body["description"])
        self.assertTrue(body["description"].endswith("Not Powered By www.gearaut.com 🍚"))
        self.assertIn("体感温度: 20.78°C - 23.93°C", body["description"])
        self.assertEqual(body["description"].count("体感温度:"), 1)
        self.assertIn("https://summitbag.com/example", body["description"])
        self.clear_requests()
        self.scenario["activity"]["description"] = body["description"]
        self.scenario["fresh"]["description"] = body["description"]
        self.assert_success(self.sync("--force"))
        self.assertEqual(self.requests()[-1]["body"]["description"].count("体感温度:"), 1)
        self.assertEqual(self.requests()[-1]["body"]["description"], body["description"])

    def test_provider_flag_overrides_config_and_qweather_blocks_are_recognized(self):
        self.configure_qweather()
        self.assert_success(self.sync("--weather-provider", "open-meteo"))
        self.assertIn("open-meteo.com/", next(entry["url"] for entry in self.requests() if entry["kind"] == "weather"))
        self.state.unlink()
        self.clear_requests()
        for apparent in ("", "  体感温度: 20.00°C - 22.00°C"):
            with self.subTest(apparent=apparent):
                self.state.unlink(missing_ok=True)
                self.clear_requests()
                self.scenario["activity"]["description"] = (
                    f"温度: 20.00°C - 22.00°C{apparent}\n湿度: 60.00% - 70.00%  风速: 3.00 km/h - 5.00 km/h\n"
                    "Powered By QWeather (www.qweather.com)")
                self.assert_success(self.sync())
                self.assertEqual(json.loads(self.state.read_text())["activities"], {"123": "existing"})
                self.assertFalse(any(entry["kind"] == "weather" for entry in self.requests()))

    def test_switching_from_qweather_preserves_notes_and_removes_old_block(self):
        self.scenario["fresh"]["description"] += (
            "\n温度: 20.00°C - 22.00°C\n湿度: 60.00% - 70.00%  风速: 3.00 km/h - 5.00 km/h\n"
            "Powered By QWeather (www.qweather.com)\n末尾备注")
        self.assert_success(self.sync("--force"))
        description = self.requests()[-1]["body"]["description"]
        self.assertNotIn("Powered By QWeather", description)
        self.assertIn("末尾备注", description)
        self.assertIn("https://summitbag.com/example", description)

    def test_qweather_unavailable_and_rate_limited_work_remains_pending(self):
        self.configure_qweather()
        for payload in ({"code": "429"}, {"code": "402"}, {"code": "200", "weatherHourly": []}):
            with self.subTest(payload=payload):
                self.scenario["qweather"] = payload
                self.assertNotEqual(self.sync().returncode, 0)
                state = json.loads(self.state.read_text())
                self.assertEqual(state["activities"], {})
                self.assertIn("123", state["pending"])
                self.assertFalse(any(entry.get("method") == "PUT" for entry in self.requests()))
        self.configure_qweather()
        self.assert_success(self.sync())

    def test_qweather_dry_run_preserves_state(self):
        self.configure_qweather()
        self.assert_success(self.sync("--dry-run"))
        self.assertFalse(self.state.exists())
        self.assertFalse(any(entry.get("method") == "PUT" for entry in self.requests()))

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

    def test_partial_weather_block_is_completed(self):
        self.scenario["activity"] = dict(self.activity, description="温度: 20°C\nPowered By www.gearaut.com")
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
    def load_module(self):
        spec = importlib.util.spec_from_file_location("strava_sync", ROOT / "strava_sync.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_current_and_legacy_weather_signatures_are_recognized(self):
        module = self.load_module()
        for signature in ("Not Powered By www.gearaut.com 🍚", "Not Powered By www.gearaut.com 👻",
                          "Powered By www.gearaut.com", "Powered By QWeather (www.qweather.com)"):
            for newline in ("\n", "\r\n"):
                with self.subTest(signature=signature, newline=newline):
                    block = BLOCK.replace("Powered By www.gearaut.com", signature).replace("\n", newline)
                    self.assertTrue(module.has_weather("原备注" + newline + block))

    def test_steadman_formula_and_wind_units(self):
        module = self.load_module()
        for arguments, expected in (((0, 0, 0), -4), ((0, 100, 0), -1.98535),
                                    ((20, 0, 36), 9), ((30, 70, 18), 32.268)):
            with self.subTest(arguments=arguments):
                self.assertAlmostEqual(module.steadman_apparent_temperature(*arguments), expected, places=3)
        for arguments in ((None, 50, 3), (True, 50, 3), (float("nan"), 50, 3),
                          (20, -1, 3), (20, 101, 3), (20, 50, -1), (20, 50, float("inf"))):
            with self.subTest(arguments=arguments), self.assertRaises(module.SyncError):
                module.steadman_apparent_temperature(*arguments)

    def test_qweather_steadman_calculates_from_interpolated_inputs_and_interior_samples(self):
        module = self.load_module()
        start = dt.datetime(2026, 10, 12, 8, tzinfo=dt.timezone.utc)
        activity = {"start_date": (start + dt.timedelta(seconds=900)).isoformat(),
                    "start_date_local": "2026-10-12T16:15:00Z", "moving_time": 5400}
        series = {"time": [start.timestamp(), start.timestamp() + 3600, start.timestamp() + 7200],
                  "temperature_2m": [20, 30, 20], "relative_humidity_2m": [40, 80, 40],
                  "wind_speed_10m": [3.6, 10.8, 3.6], "weather_code": [0, 0, 0]}
        with patch.object(module, "qweather_series", return_value=series):
            emoji, description = module.weather_annotation(activity, (39.9, 116.4), "qweather")
        self.assertEqual(emoji, "🌞🥵")
        self.assertIn("体感温度: 21.93°C - 35.06°C", description)
        self.assertTrue(module.has_weather(description))
        for field in ("temperature_2m", "relative_humidity_2m", "wind_speed_10m"):
            incomplete = dict(series, **{field: [20, None, 20]})
            with self.subTest(field=field), patch.object(module, "qweather_series", return_value=incomplete):
                with self.assertRaises(module.SyncError):
                    module.weather_annotation(activity, (39.9, 116.4), "qweather")

    def test_qweather_steadman_heat_and_cold_emoji_thresholds(self):
        module = self.load_module()
        start = dt.datetime(2026, 10, 12, 8, tzinfo=dt.timezone.utc)
        activity = {"start_date": start.isoformat(), "start_date_local": "2026-10-12T16:00:00Z", "moving_time": 0}
        for temperature, humidity, wind, expected in ((30, 70, 18, "🌞🥵"), (5, 50, 18, "🌞🥶"),
                                                       (34, 0, 0, "🌞"), (4, 0, 0, "🌞")):
            series = {"time": [start.timestamp()], "temperature_2m": [temperature],
                      "relative_humidity_2m": [humidity], "wind_speed_10m": [wind], "weather_code": [0]}
            with self.subTest(temperature=temperature, humidity=humidity), \
                    patch.object(module, "qweather_series", return_value=series):
                self.assertEqual(module.weather_annotation(activity, (39.9, 116.4), "qweather")[0], expected)

    def test_qweather_cross_day_requests_use_city_timezone_and_complete_boundaries(self):
        module = self.load_module()
        start = dt.datetime(2026, 10, 12, 15, 50, tzinfo=dt.timezone.utc).timestamp()
        now = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
        calls = []

        def request(path, parameters):
            calls.append((path, parameters))
            if path.startswith("/geo/"):
                return {"location": [{"id": "101010100", "tz": "Asia/Shanghai"}]}
            date = "2026-10-12" if parameters["date"] == "20261012" else "2026-10-13"
            hours = [23] if date.endswith("12") else [0, 1]
            return {"weatherHourly": [
                {"time": f"{date} {hour:02d}:00", "temp": "20", "humidity": "60",
                 "windSpeed": "3", "icon": "305"} for hour in hours]}

        with patch.object(module, "qweather_request", side_effect=request):
            series = module.qweather_series((39.9, 116.4), start, start + 1200, now=now)
        self.assertEqual([parameters["date"] for path, parameters in calls[1:]], ["20261012", "20261013"])
        self.assertEqual(series["time"], [start - 3000, start + 600, start + 4200])
        self.assertEqual(series["weather_code"], [61, 61, 61])

    def test_qweather_today_and_expired_history_are_retryable(self):
        module = self.load_module()
        now = dt.datetime(2026, 10, 15, 8, tzinfo=dt.timezone.utc)
        for start in (now.timestamp() - 3600, now.timestamp() - 12 * 86400):
            with patch.object(module, "qweather_request", return_value={
                    "location": [{"id": "101010100", "tz": "Asia/Shanghai"}]}) as request:
                with self.assertRaises(module.SyncError):
                    module.qweather_series((39.9, 116.4), start, start + 1200, now=now)
                self.assertEqual(request.call_count, 1)

    def test_qweather_icon_categories(self):
        module = self.load_module()
        for icon, expected in ((100, 0), (150, 0), (101, 2), (153, 2), (104, 3), (302, 95), (399, 61),
                               (456, 71), (504, 45), (507, 45)):
            self.assertEqual(module.qweather_code(str(icon)), expected)
        with self.assertRaises(module.SyncError):
            module.qweather_code("999")

    def test_qweather_auth_gzip_and_safe_errors(self):
        import gzip
        module = self.load_module()
        environment = {"QWEATHER_API_HOST": "test.qweatherapi.com", "QWEATHER_API_KEY": "test-weather-key"}
        for auth in ({"QWEATHER_API_KEY": "test-weather-key"}, {"QWEATHER_JWT": "test-jwt"}):
            with patch.dict(os.environ, {**environment, "QWEATHER_API_KEY": "", **auth}, clear=True), \
                    patch.object(module.urllib.request, "urlopen", return_value=io.BytesIO(
                        gzip.compress(b'{"code":"200"}'))) as request:
                self.assertEqual(module.qweather_request("/v7/historical/weather", {}), {"code": "200"})
                outgoing = request.call_args.args[0]
                self.assertNotIn(next(iter(auth.values())), outgoing.full_url)
                header = "Authorization" if "QWEATHER_JWT" in auth else "X-qw-api-key"
                self.assertIn(next(iter(auth.values())), outgoing.get_header(header))
        for status in (401, 403, 429):
            with patch.dict(os.environ, environment, clear=True), patch.object(
                    module.urllib.request, "urlopen", side_effect=urllib.error.HTTPError(
                        "https://test.qweatherapi.com/", status, "test-weather-key", {}, None)):
                with self.assertRaises(module.StopBatchError) as error:
                    module.qweather_request("/v7/historical/weather", {})
                self.assertNotIn("test-weather-key", str(error.exception))

    def test_qweather_fractional_timezone_requests_next_local_day_for_interpolation(self):
        module = self.load_module()
        start = dt.datetime(2026, 10, 12, 17, 35, tzinfo=dt.timezone.utc).timestamp()
        calls = []

        def request(path, parameters):
            calls.append(parameters)
            if path.startswith("/geo/"):
                return {"location": [{"id": "city-id", "tz": "Asia/Kathmandu"}]}
            time = "2026-10-12 23:00" if parameters["date"] == "20261012" else "2026-10-13 00:00"
            return {"weatherHourly": [{"time": time, "temp": "20", "humidity": "60", "windSpeed": "3", "icon": "100"}]}

        with patch.object(module, "qweather_request", side_effect=request):
            series = module.qweather_series((27.7, 85.3), start, start + 600,
                                           now=dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc))
        self.assertEqual([parameters["date"] for parameters in calls[1:]], ["20261012", "20261013"])
        self.assertEqual(module.interval_extrema(series["time"], series["temperature_2m"], start, start + 600), (20, 20))

    def test_qweather_config_rejects_invalid_host_and_header_values(self):
        module = self.load_module()
        for host, key in (("", "test"), ("https://test.qweatherapi.com", "test"),
                          ("attacker.example", "test"), ("test.qweatherapi.com", ""),
                          ("test.qweatherapi.com", "secret\nHeader: injected")):
            with patch.dict(os.environ, {"QWEATHER_API_HOST": host, "QWEATHER_API_KEY": key}, clear=True):
                with self.assertRaises(module.SyncError):
                    module.qweather_config()

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
