#!/usr/bin/env python3
"""Security contracts exercised with synthetic credentials and mocked Strava responses."""

import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SECRET = "dummy-client-secret"
ACCESS = "dummy-access-token"
REFRESH = "dummy-refresh-token"

CURL_MOCK = r'''#!/usr/bin/env python3
import json, os, stat, sys
from pathlib import Path
from urllib.parse import parse_qs
args = sys.argv[1:]
data = sys.stdin.read()
url = next(arg for arg in args if arg.startswith("https://"))
method = args[args.index("-X") + 1] if "-X" in args else "GET"
entry = {"args": args, "method": method, "url": url}
def record():
    with open("requests.jsonl", "a") as log:
        log.write(json.dumps(entry) + "\n")
if url.endswith("/oauth/token"):
    form = parse_qs(data)
    if data:
        assert form["client_secret"] == ["dummy-client-secret"]
        assert form["grant_type"][0] in ("refresh_token", "authorization_code")
    record()
    print(json.dumps({"access_token": "dummy-new-access-token",
                      "refresh_token": "dummy-new-refresh-token", "expires_at": 4102444800}))
    sys.exit(0)
output = Path(args[args.index("-o") + 1])
runtime = Path(os.environ["TMPDIR"]).resolve()
if runtime not in output.resolve().parents:
    record()
    sys.exit(98)  # Do not let an unfixed script write to shared /tmp paths.
entry["directory_mode"] = stat.S_IMODE(output.parent.stat().st_mode)
if data:
    expected = json.loads(Path(".strava_tokens.json").read_text())["access_token"]
    assert data.strip() == "Authorization: Bearer " + expected
if "--data-binary" in args:
    body = Path(args[args.index("--data-binary") + 1][1:])
    entry["body_mode"] = stat.S_IMODE(body.stat().st_mode)
    entry["body"] = json.loads(body.read_text())
activity = {"id": 123, "name": "晨跑", "description": "原备注", "sport_type": "Run"}
if os.environ.get("MOCK_REORDER") == "1":
    activity.update(sport_type="TrailRun", description=(
        "原备注\n温度: 10°C - 12°C\nPowered By www.gearaut.com\nhttps://summitbag.com/example"))
payload = [activity] if "/athlete/activities?" in url else activity
output.write_text(json.dumps(payload))
entry["file_mode"] = stat.S_IMODE(output.stat().st_mode)
record()
print(os.environ.get("MOCK_STATUS", "200"), end="")
'''

class ScriptSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="strava-security-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        for name in ("update_strava_weather.sh", "reorder_summitbag.sh", "strava_common.sh", "strava_oauth.py",
                     "weather_template.py", "project_config.py", "config.default.json", "api_usage.py",
                     "peaks.py", "activity_update.py"):
            source = ROOT / name
            if source.exists():
                shutil.copy2(source, self.directory)
        (self.directory / "config.local.json").write_text(json.dumps(
            {"strava": {"client_id": "dummy-client-id", "client_secret": SECRET}}))
        self.token_file = self.directory / ".strava_tokens.json"
        self.token_file.write_text(json.dumps({
            "access_token": ACCESS, "refresh_token": REFRESH, "expires_at": 4102444800,
        }))
        for path in (self.token_file, self.directory / "config.local.json"):
            path.chmod(0o644)
        (self.directory / "bin").mkdir()
        mock = self.directory / "bin/curl"
        mock.write_text(CURL_MOCK)
        mock.chmod(0o755)
        self.runtime = self.directory / "runtime"
        self.runtime.mkdir()
        self.environment = os.environ.copy()
        for name in ("ACTIVITY_ID", "YES", "DRY_RUN", "WEATHER_DESC", "WEATHER_DESC_FILE",
                     "WEATHER_EMOJI", "WEATHER_EMOJI_FILE", "WEATHER_TEMPLATE_FILE", "REORDER_SUMMITBAG",
                     "STRAVA_SYNC_ACTIVITY_FILE", "STRAVA_REORDER_ONLY", "CLIENT_SECRET", "CLIENT_ID"):
            self.environment.pop(name, None)
        self.environment.update(PATH=f'{self.directory / "bin"}:{os.environ["PATH"]}',
                                PYTHONPATH=str(self.directory), TMPDIR=str(self.runtime))

    def run_script(self, script, *args, trace=False, input_text="", **environment):
        command = ["bash", "-x", script] if trace else ["bash", script]
        return subprocess.run(command + list(args), cwd=self.directory,
                              env=dict(self.environment, MOCK_REORDER="1" if script == "reorder_summitbag.sh" else "0",
                                       **environment), input=input_text,
                              text=True, capture_output=True, timeout=15)

    def requests(self):
        path = self.directory / "requests.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_existing_credentials_are_private_on_both_entrypoints(self):
        for script, args in (("update_strava_weather.sh", ("list",)), ("reorder_summitbag.sh", ())):
            with self.subTest(script=script):
                for path in (self.token_file, self.directory / "config.local.json"):
                    path.chmod(0o644)
                self.assert_success(self.run_script(script, *args))
                for path in (self.token_file, self.directory / "config.local.json"):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_token_exchange_and_both_refresh_paths_save_private_tokens(self):
        for script, args in (("update_strava_weather.sh", ("auth",)),
                             ("update_strava_weather.sh", ("list",)),
                             ("reorder_summitbag.sh", ())):
            with self.subTest(script=script, args=args):
                self.token_file.write_text(json.dumps({
                    "access_token": ACCESS, "refresh_token": REFRESH, "expires_at": 0,
                }))
                self.token_file.chmod(0o644)
                result = self.run_script(script, *args, input_text="dummy-code\n")
                self.assert_success(result)
                self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)
                self.assertEqual(json.loads(self.token_file.read_text())["refresh_token"],
                                 "dummy-new-refresh-token")
                self.assertEqual(list(self.directory.glob(".strava_tokens.*")), [self.token_file])

    def test_curl_arguments_and_shell_trace_do_not_expose_credentials(self):
        for script, args in (("update_strava_weather.sh", ("auth",)),
                             ("update_strava_weather.sh", ("get",)),
                             ("reorder_summitbag.sh", ())):
            with self.subTest(script=script, args=args):
                self.token_file.write_text(json.dumps({
                    "access_token": ACCESS, "refresh_token": REFRESH,
                    "expires_at": 0 if script == "reorder_summitbag.sh" else 4102444800,
                }))
                result = self.run_script(script, *args, trace=True, ACTIVITY_ID="123", input_text="dummy-code\n")
                self.assert_success(result)
                arguments = json.dumps([entry.get("args", []) for entry in self.requests()])
                for value in (SECRET, ACCESS, REFRESH, "dummy-code", "dummy-new-access-token", "dummy-new-refresh-token"):
                    self.assertNotIn(value, arguments)
                    self.assertNotIn(value, result.stdout + result.stderr)

    def test_updates_use_private_files_and_clean_up_after_success_or_failure(self):
        for status in ("200", "500"):
            with self.subTest(status=status):
                (self.directory / "requests.jsonl").unlink(missing_ok=True)
                result = self.run_script("update_strava_weather.sh", ACTIVITY_ID="123", YES="1",
                                         WEATHER_EMOJI="🌞", WEATHER_DESC="天气说明", MOCK_STATUS=status)
                self.assertEqual(result.returncode == 0, status == "200", result.stderr)
                requests = self.requests()
                self.assertEqual([entry["method"] for entry in requests],
                                 ["GET", "PUT"] if status == "200" else ["GET"])
                for entry in requests:
                    self.assertEqual(entry["directory_mode"], 0o700)
                    self.assertEqual(entry["file_mode"], 0o600)
                    if entry["method"] == "PUT":
                        self.assertEqual(entry["body_mode"], 0o600)
                        self.assertEqual(entry["body"], {"name": "晨跑 🌞", "description": "原备注\n天气说明"})
                self.assertEqual(list(self.runtime.iterdir()), [])

    def test_dry_run_prevents_writes_even_with_yes(self):
        for script, args in (("update_strava_weather.sh", ()), ("reorder_summitbag.sh", ())):
            with self.subTest(script=script):
                (self.directory / "requests.jsonl").unlink(missing_ok=True)
                self.assert_success(self.run_script(script, *args, ACTIVITY_ID="123", YES="1", DRY_RUN="1"))
                self.assertTrue(self.requests())
                self.assertTrue(all(entry["method"] == "GET" for entry in self.requests()))

    def test_manual_updates_send_only_changed_fields_and_skip_noop(self):
        for emoji, description, expected in (("", "天气说明", {"description": "原备注\n天气说明"}),
                                             ("🌞", "", {"name": "晨跑 🌞"}), ("", "", None)):
            with self.subTest(emoji=emoji, description=description):
                (self.directory / "requests.jsonl").unlink(missing_ok=True)
                self.assert_success(self.run_script("update_strava_weather.sh", ACTIVITY_ID="123", YES="1",
                                                     WEATHER_EMOJI=emoji, WEATHER_DESC=description))
                writes = [entry["body"] for entry in self.requests() if entry["method"] == "PUT"]
                self.assertEqual(writes, [expected] if expected else [])
                self.assertEqual(list(self.runtime.iterdir()), [])

    def test_manual_updates_use_the_shared_title_template(self):
        path = self.directory / "config.local.json"
        config = json.loads(path.read_text())
        config["title"] = {"template": "{emoji} | {title}"}
        path.write_text(json.dumps(config))
        self.assert_success(self.run_script("update_strava_weather.sh", ACTIVITY_ID="123", YES="1", WEATHER_EMOJI="🌞"))
        writes = [entry["body"] for entry in self.requests() if entry["method"] == "PUT"]
        self.assertEqual(writes, [{"name": "🌞 | 晨跑"}])

    def test_snapshot_must_match_the_target_activity(self):
        snapshot = self.directory / "snapshot.json"
        snapshot.write_text(json.dumps({"id": 999, "name": "其它活动", "description": "重要备注"}))
        result = self.run_script("update_strava_weather.sh", ACTIVITY_ID="123", YES="1", WEATHER_DESC="天气说明",
                                 STRAVA_SYNC_ACTIVITY_FILE=str(snapshot))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ID", result.stderr)
        self.assertEqual(self.requests(), [])

    def test_reorder_still_writes_when_explicitly_enabled(self):
        self.assert_success(self.run_script("reorder_summitbag.sh", YES="1"))
        self.assertIn("PUT", [entry["method"] for entry in self.requests()])

    def test_reorder_requests_share_usage_tracking(self):
        self.assert_success(self.run_script("reorder_summitbag.sh", YES="1"))
        usage = json.loads((self.directory / ".strava_api_usage.json").read_text())
        self.assertEqual(usage["read_daily"], 3)
        self.assertEqual([entry["method"] for entry in self.requests()], ["GET", "GET", "PUT"])

    def test_reorder_budget_pause_prevents_requests(self):
        import datetime as dt
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        (self.directory / ".strava_api_usage.json").write_text(json.dumps({
            "daily_date": today, "read_daily": 1800, "window": [],
        }))
        result = self.run_script("reorder_summitbag.sh", YES="1")
        self.assertEqual(result.returncode, 4, result.stderr + result.stdout)
        self.assertEqual(self.requests(), [])

    def test_reorder_and_read_use_shared_paging_defaults(self):
        path = self.directory / "config.local.json"
        config = json.loads(path.read_text())
        config["strava"]["list_count"] = 7
        config["reorder"] = {"page_size": 17, "request_interval_milliseconds": 0}
        path.write_text(json.dumps(config))
        self.assert_success(self.run_script("update_strava_weather.sh", "list"))
        self.assertIn("per_page=7", self.requests()[0]["url"])
        self.assert_success(self.run_script("reorder_summitbag.sh"))
        self.assertTrue(any("per_page=17" in entry["url"] for entry in self.requests()))

    def test_symlinked_token_cache_is_rejected_without_changing_target(self):
        target = self.directory / "other.json"
        self.token_file.rename(target)
        target.chmod(0o644)
        before = target.read_bytes()
        self.token_file.symlink_to(target)
        for script, args in (("update_strava_weather.sh", ("list",)), ("reorder_summitbag.sh", ())):
            result = self.run_script(script, *args)
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        self.assertEqual(self.requests(), [])


class OAuthSecurityTests(unittest.TestCase):
    def load_module(self):
        spec = importlib.util.spec_from_file_location("strava_oauth", ROOT / "strava_oauth.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_invalid_state_and_unrelated_requests_do_not_consume_login(self):
        module = self.load_module()
        errors = []
        states = []
        threads = []

        def open_browser(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            state = query["state"][0]
            states.append(state)
            self.assertGreaterEqual(len(state), 32)
            callback = query["redirect_uri"][0]
            self.assertEqual(urllib.parse.urlsplit(callback).hostname, "127.0.0.1")

            def callback_requests():
                try:
                    for suffix in ("?code=wrong", "?" + urllib.parse.urlencode(
                            {"code": "wrong", "state": "错误"}), "/favicon.ico"):
                        try:
                            urllib.request.urlopen(callback + suffix, timeout=2)
                        except urllib.error.HTTPError as error:
                            self.assertIn(error.code, (400, 404))
                            error.close()
                        else:
                            self.fail("untrusted callback was accepted")
                    with urllib.request.urlopen(callback + "?" + urllib.parse.urlencode(
                            {"code": "valid-code", "state": state}), timeout=2) as response:
                        self.assertEqual(response.status, 200)
                except BaseException as error:
                    errors.append(error)
            thread = threading.Thread(target=callback_requests, daemon=True)
            threads.append(thread)
            thread.start()
            return True

        for _ in range(2):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(module.authorize("dummy-client-id", timeout=5, opener=open_browser), "valid-code")
        for thread in threads:
            thread.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(len(set(states)), 2)

    def test_authorization_timeout_is_bounded(self):
        with self.assertRaisesRegex(RuntimeError, "超时"):
            with contextlib.redirect_stderr(io.StringIO()):
                self.load_module().authorize("dummy-client-id", timeout=0.05, opener=lambda url: True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
