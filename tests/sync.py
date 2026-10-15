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
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
BLOCK = ("温度: 20.00°C - 22.00°C  体感温度: 30.00°C - 32.00°C\n"
         "湿度: 60.00% - 70.00%  风速: 3.00 km/h - 5.00 km/h\nPowered By www.gearaut.com")

CURL_MOCK = r'''#!/usr/bin/env python3
import json, sys
from pathlib import Path
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
if method == "PUT":
    body = json.loads(Path(args[args.index("--data-binary") + 1][1:]).read_text())
    entry["body"] = body
    status = str(scenario.get("put_status", 200))
    payload = dict(body, id=scenario.get("put_id", 123))
elif "/athlete/activities?" in url:
    payload = [] if scenario.get("empty") else [scenario["activity"]]
elif "/streams?" in url:
    payload = scenario.get("streams", {"latlng": {"data": [None, [39.9, 116.4]]}})
    status = str(scenario.get("streams_status", 200))
else:
    count = sum(entry.get("kind") == "strava" and entry.get("method") == "GET"
                and entry["url"].endswith("/activities/123") for entry in previous)
    payload = scenario.get("fresh", scenario["activity"]) if count else scenario["activity"]
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
                     "WEATHER_EMOJI", "WEATHER_EMOJI_FILE", "CLIENT_ID", "CLIENT_SECRET"):
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
                              capture_output=True, timeout=15)

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
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(list(self.runtime.iterdir()), [])
        self.clear_requests()
        self.assert_success(self.sync())
        self.assertEqual(len(self.requests()), 1)
        self.assertIn("/athlete/activities?", self.requests()[0]["url"])

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
        self.scenario["activity"] = dict(self.activity, description=BLOCK)
        self.assert_success(self.sync())
        self.clear_requests()
        self.assert_success(self.sync("--force"))
        self.assertEqual(self.requests()[-1]["method"], "PUT")

    def test_failures_are_not_cached_and_can_be_retried(self):
        for failure in ({"weather_error": True}, {"put_status": 500}, {"put_id": 999}):
            with self.subTest(failure=failure):
                self.state.unlink(missing_ok=True)
                self.scenario.update(failure)
                self.assertNotEqual(self.sync().returncode, 0)
                self.assertFalse(self.state.exists())
                for key in failure:
                    self.scenario.pop(key)
        self.assert_success(self.sync())
        self.assertTrue(self.state.exists())

    def test_empty_activity_list_does_not_create_processed_state(self):
        self.scenario["empty"] = True
        self.assert_success(self.sync())
        self.assertEqual(len(self.requests()), 1)
        self.assertFalse(self.state.exists())

    def test_coordinates_use_stream_fallback_and_allow_explicit_location(self):
        self.scenario["activity"] = dict(self.activity, start_latlng=[])
        self.assert_success(self.sync())
        self.assertTrue(any("/streams?" in entry["url"] for entry in self.requests()))
        self.state.unlink()
        self.clear_requests()
        self.scenario["streams_status"] = 404
        self.assertNotEqual(self.sync().returncode, 0)
        self.assertFalse(self.state.exists())
        self.clear_requests()
        self.assert_success(self.sync("--latitude", "39.9", "--longitude", "116.4"))
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
        self.assertFalse(self.state.exists())

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


class WeatherMathTests(unittest.TestCase):
    def load_module(self):
        spec = importlib.util.spec_from_file_location("strava_sync", ROOT / "strava_sync.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
