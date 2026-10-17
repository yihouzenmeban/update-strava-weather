#!/usr/bin/env python3
"""Exercise local HTTP webhook -> detached worker -> real sync with fake APIs."""

import http.client
from http.server import HTTPServer
import importlib.util
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import unittest
from unittest.mock import patch

from sync import ROOT, SyncFixture


LEGACY = ("温度: 13.58°C - 21.48°C 体感温度: 13.60°C - 22.50°C\n"
          "湿度: 89% - 69% 风速: 2.98 km/h - 3.95 km/h\nPowered By www.gearaut.com")
DEFAULT = ("温度: 22.00°C - 26.00°C  体感温度: 32.00°C - 36.00°C\n"
           "湿度: 70.00% - 90.00%  风速: 5.00 km/h - 9.00 km/h\n"
           "Not Powered By www.gearaut.com 🍚")
MOUNTAINS = ("⛰️ 馒头峪 (767 m) • ⛰️ 萝芭地北尖 (1,146 m) • ⛰️ Yangtai Shan (1,276 m) • "
             "⛰️ Miaofeng Shan (1,291 m) • ⛰️ 冷风口 (919 m) | 🌐 summitbag.com")
EVENT = {"object_type": "activity", "aspect_type": "create", "object_id": 123,
         "owner_id": 456, "subscription_id": 789, "event_time": 1790000000, "updates": {}}

SLEEP_MOCK = '''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
assert sys.argv[1:] == ["60"], sys.argv
with Path("webhook-delays.jsonl").open("a") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
gate = os.environ.get("WEBHOOK_TEST_GATE")
deadline = time.monotonic() + 10
while gate and not Path(gate).exists():
    if time.monotonic() > deadline:
        raise TimeoutError("test did not release the delayed worker")
    time.sleep(0.01)
'''


class WebhookTests(SyncFixture):
    def setUp(self):
        super().setUp()
        hook = self.directory / "webhook"
        (hook / "logs").mkdir(parents=True)
        shutil.copy2(ROOT / "webhook/receiver.py", hook / "receiver.py")
        (hook / "receiver_config.json").write_text(json.dumps({"verify_token": "synthetic-webhook-token"}))
        spec = importlib.util.spec_from_file_location("receiver_under_test", hook / "receiver.py")
        self.receiver = importlib.util.module_from_spec(spec)
        with patch("logging.basicConfig"), patch("logging.FileHandler", return_value=logging.NullHandler()):
            spec.loader.exec_module(self.receiver)
        log_patch = patch.object(self.receiver, "log")
        log_patch.start()
        self.addCleanup(log_patch.stop)
        self.assertEqual(self.receiver.SYNC_SCRIPT, (self.directory / "update_strava_weather.sh").resolve())
        sleep = self.directory / "bin/sleep"
        sleep.write_text(SLEEP_MOCK)
        sleep.chmod(0o755)
        self.environment.pop("WEBHOOK_TEST_GATE", None)
        self.children = []
        self.addCleanup(self.stop_children)
        self.server = HTTPServer(("127.0.0.1", 0), self.receiver.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            child.wait(timeout=5)

    def post_event(self, event=EVENT):
        real_popen = subprocess.Popen

        def spawn(command, **kwargs):
            self.assertEqual(command, ["/bin/bash", "-c", 'sleep 60; exec "$1" sync --activity-id "$2"',
                                       "delayed-sync", str(self.receiver.SYNC_SCRIPT), "123"])
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(Path(kwargs["cwd"]), self.directory.resolve())
            child = real_popen(command, env=self.environment, **kwargs)
            self.children.append(child)
            return child

        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            with patch.object(self.receiver.subprocess, "Popen", side_effect=spawn):
                body = event if isinstance(event, str) else json.dumps(event)
                connection.request("POST", "/strava-webhook", body=body, headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), {"ok": True})
        finally:
            connection.close()

    def wait_worker(self, expected_code=0):
        child = self.children[-1]
        code = child.wait(timeout=30)
        log = (self.directory / "webhook/logs/sync-123.log").read_text()
        self.assertEqual(code, expected_code, log)
        self.assertTrue((self.directory / "webhook-delays.jsonl").exists())
        return log

    def prepare(self, description, template=None, disabled=False, cached=False):
        self.state.unlink(missing_ok=True)
        self.clear_requests()
        config = 'CLIENT_ID="test-client-id"\nCLIENT_SECRET="test-client-secret"\n'
        if template:
            (self.directory / "custom.txt").write_text(template)
            config += 'WEATHER_TEMPLATE_FILE="custom.txt"\n'
        if disabled:
            config += 'REORDER_SUMMITBAG=0\n'
        (self.directory / "strava_config.sh").write_text(config)
        self.scenario["activity"] = dict(self.activity, description=description)
        self.scenario["fresh"] = dict(self.scenario["activity"])
        (self.directory / "scenario.json").write_text(json.dumps(self.scenario))
        if cached:
            self.state.write_text(json.dumps({"version": 2, "activities": {"123": "updated"},
                                             "pending": {}, "last_scan": None, "scan_from": None}))

    def assert_result(self, description, expected, template=None, disabled=False, cached=False,
                      creates_weather=False):
        self.prepare(description, template, disabled, cached)
        before = len(self.children)
        self.post_event()
        self.assertEqual(len(self.children), before + 1)
        self.wait_worker()
        requests = self.requests()
        writes = [entry["body"] for entry in requests if entry.get("method") == "PUT"]
        self.assertEqual(len(writes), int(expected != description))
        if writes:
            self.assertEqual(writes[0]["description"], expected)
            self.assertEqual(writes[0]["name"], "晨跑 🌧️🥵" if creates_weather else "晨跑")
        self.assertEqual(sum(entry["kind"] == "weather" for entry in requests), int(creates_weather))
        self.assertEqual(sum(entry["kind"] == "strava" for entry in requests), 3 if writes else 1)
        self.assertFalse(any("/athlete/activities?" in entry["url"] for entry in requests))

    def test_create_event_sorts_varied_existing_weather_formats(self):
        for label, weather in (("用户提供的旧版格式", LEGACY), ("当前默认模板", DEFAULT),
                               ("历史幽灵署名", DEFAULT.replace("🍚", "👻")),
                               ("历史 QWeather 署名", LEGACY.replace("www.gearaut.com", "QWeather (www.qweather.com)"))):
            with self.subTest(format=label):
                self.assert_result(weather + "\n\n" + MOUNTAINS, MOUNTAINS + "\n" + weather)

    def test_create_event_handles_multiline_mountains_notes_and_crlf(self):
        peaks = "⛰️ 馒头峪 (767 m)\n🏔️ 萝芭地北尖 (1,146 m)\n🌐 summitbag.com"
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                source = "原备注\n" + LEGACY + "\n重要后记\n" + peaks + "\n最后的备注"
                expected = "原备注\n" + peaks + "\n" + LEGACY + "\n重要后记\n最后的备注"
                self.assert_result(source.replace("\n", newline), expected.replace("\n", newline))

    def test_create_event_sorts_custom_template_and_emoji_only_mountain_lines(self):
        template = "⛰️ 跑山天气 {emoji}\n气温 {t_min:.1f}～{t_max:.1f}°C"
        weather = "⛰️ 跑山天气 🌙\n气温 -2.0～5.0°C"
        peaks = "⛰ 馒头峪 (767 m)\n🏔️ Yangtai Shan (1,276 m)"
        self.assert_result("原备注\n" + weather + "\n" + peaks,
                           "原备注\n" + peaks + "\n" + weather, template=template)

    def test_create_event_keeps_correct_or_disabled_descriptions_unchanged(self):
        for label, source, disabled in (("顺序已正确", MOUNTAINS + "\n" + DEFAULT, False),
                                       ("没有山峰", DEFAULT + "\n普通后记", False),
                                       ("关闭排序", DEFAULT + "\n" + MOUNTAINS, True),
                                       ("无法定位旧天气", "旧天气 www.gearaut.com\n" + MOUNTAINS, False)):
            with self.subTest(format=label):
                self.assert_result(source, source, disabled=disabled)

    def test_create_event_adds_missing_weather_and_keeps_mountains_first(self):
        for source in (MOUNTAINS, "原备注"):
            with self.subTest(description=source):
                self.assert_result(source, source + "\n" + DEFAULT, creates_weather=True)

    def test_duplicate_create_event_rechecks_cached_activity_without_duplicate_writes(self):
        self.assert_result(LEGACY + "\n" + MOUNTAINS, MOUNTAINS + "\n" + LEGACY, cached=True)
        body = self.requests()[-1]["body"]
        self.scenario["activity"].update(body)
        self.scenario["fresh"].update(body)
        (self.directory / "scenario.json").write_text(json.dumps(self.scenario))
        self.clear_requests()
        self.post_event()
        self.wait_worker()
        self.assertEqual(len(self.requests()), 1)
        self.assertTrue(all(entry.get("method") == "GET" for entry in self.requests()))

    def test_non_create_events_are_acknowledged_without_triggering_sync(self):
        for object_type, aspect_type in (("activity", "update"), ("activity", "delete"), ("athlete", "update")):
            with self.subTest(object_type=object_type, aspect_type=aspect_type):
                self.post_event(dict(EVENT, object_type=object_type, aspect_type=aspect_type))
        self.post_event("invalid JSON")
        self.assertEqual(self.children, [])
        self.assertEqual(self.requests(), [])

    def test_http_acknowledges_create_while_delayed_worker_is_still_waiting(self):
        self.prepare(DEFAULT)
        gate = self.directory / "release-worker"
        self.environment["WEBHOOK_TEST_GATE"] = str(gate)
        try:
            self.post_event()
            self.assertEqual(len(self.children), 1)
            self.assertIsNone(self.children[0].poll())
            self.assertEqual(self.requests(), [])
        finally:
            gate.touch()
        self.wait_worker()

    def test_failed_worker_keeps_pending_activity_and_duplicate_event_retries(self):
        self.prepare(LEGACY + "\n" + MOUNTAINS, cached=True)
        self.scenario["put_status"] = 500
        (self.directory / "scenario.json").write_text(json.dumps(self.scenario))
        self.post_event()
        self.wait_worker(expected_code=1)
        self.assertEqual(json.loads(self.state.read_text())["pending"], {"123": False})
        self.scenario.pop("put_status")
        (self.directory / "scenario.json").write_text(json.dumps(self.scenario))
        self.clear_requests()
        self.post_event()
        self.wait_worker()
        self.assertEqual(self.requests()[-1]["body"]["description"], MOUNTAINS + "\n" + LEGACY)
        self.assertFalse(any(entry["kind"] == "weather" for entry in self.requests()))
        self.assertEqual(json.loads(self.state.read_text())["pending"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
