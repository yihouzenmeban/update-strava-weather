#!/usr/bin/env python3
"""Shared defaults, private overrides and legacy migration; no network access."""

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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="strava-config-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copy2(ROOT / "config.default.json", self.root)
        from project_config import ensure_verify_token, load_config, migrate_legacy
        self.load = lambda **kwargs: load_config(self.root, environ=kwargs.get("environ", {}))
        self.ensure_token = lambda: ensure_verify_token(self.root)
        self.migrate = lambda: migrate_legacy(self.root)
        self.local = self.root / "config.local.json"

    def write(self, data):
        self.local.write_text(json.dumps(data, ensure_ascii=False))

    def test_no_local_config_uses_defaults_without_creating_a_file(self):
        config = self.load()
        self.assertEqual(config["webhook"]["sync_delay_seconds"], 60)
        self.assertTrue(config["weather"]["reorder_summitbag"])
        self.assertEqual(config["sync"]["page_size"], 50)
        self.assertIn("Not Powered By www.gearaut.com 🍚", config["weather"]["template"])
        self.assertFalse(self.local.exists())

    def test_partial_overrides_preserve_defaults_and_false_zero_empty_values(self):
        self.write({"weather": {"reorder_summitbag": False},
                    "webhook": {"sync_delay_seconds": 0, "hooks": []}})
        config = self.load()
        self.assertFalse(config["weather"]["reorder_summitbag"])
        self.assertEqual(config["webhook"]["sync_delay_seconds"], 0)
        self.assertEqual(config["webhook"]["hooks"], [])
        self.assertEqual(config["sync"]["page_size"], 50)
        self.assertIn("{t_min:.2f}", config["weather"]["template"])

    def test_title_defaults_and_partial_customization(self):
        self.assertEqual(self.load()["title"], {"enabled": True, "template": "{title} {emoji}"})
        self.write({"title": {"template": "{emoji} | {title}"}})
        self.assertEqual(self.load()["title"], {"enabled": True, "template": "{emoji} | {title}"})
        self.write({"title": {"enabled": False}})
        self.assertFalse(self.load()["title"]["enabled"])

    def test_map_style_defaults_and_validation(self):
        self.assertEqual(self.load()["map_style"], {"enabled": True, "power": "power", "no_power": "pace"})
        self.write({"map_style": {"power": "heartrate"}})
        self.assertEqual(self.load()["map_style"]["power"], "heartrate")
        self.write({"map_style": {"no_power": "bogus"}})
        with self.assertRaisesRegex(ValueError, "map_style"):
            self.load()
        self.write({"map_style": {"enabled": "yes"}})
        with self.assertRaisesRegex(ValueError, "map_style"):
            self.load()

    def test_invalid_title_templates_are_rejected(self):
        for template in ("", "{emoji}", "{title} {title}", "{title} {emoji}{emoji}",
                         "{title.real}", "{title!r}", "{title:>10}", "{title}\n{emoji}", "{title} {unknown}", "{title"):
            with self.subTest(template=template):
                self.write({"title": {"template": template}})
                with self.assertRaisesRegex(ValueError, "标题模板"):
                    self.load()

    def test_environment_overrides_custom_config_with_typed_values(self):
        self.write({"weather": {"reorder_summitbag": False}, "webhook": {"sync_delay_seconds": 120}})
        config = self.load(environ={"REORDER_SUMMITBAG": "1", "WEBHOOK_SYNC_DELAY_SECONDS": "0",
                                    "CLIENT_ID": "env-client", "WEATHER_TEMPLATE_FILE": "custom.txt"})
        self.assertTrue(config["weather"]["reorder_summitbag"])
        self.assertEqual(config["webhook"]["sync_delay_seconds"], 0)
        self.assertEqual(config["strava"]["client_id"], "env-client")
        self.assertEqual(config["weather"]["template_file"], "custom.txt")

    def test_invalid_config_is_reported_without_leaking_values(self):
        for data in ({"webhook": {"sync_delay_seconds": -1}}, {"webhook": {"sync_delay_seconds": True}},
                     {"sync": {"page_size": 0}}, {"weather": {"reorder_summitbag": "secret-value"}},
                     {"unknown": "secret-value"}, {"strava": []}, []):
            with self.subTest(data=data):
                self.write(data)
                with self.assertRaises(ValueError) as caught:
                    self.load()
                self.assertNotIn("secret-value", str(caught.exception))
        self.local.write_text('{"strava": "secret-value"')
        with self.assertRaises(ValueError) as caught:
            self.load()
        self.assertNotIn("secret-value", str(caught.exception))

    def test_invalid_environment_has_an_actionable_error(self):
        for name, value in (("WEBHOOK_SYNC_DELAY_SECONDS", "-1"), ("REORDER_SUMMITBAG", "true")):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, name):
                self.load(environ={name: value})

    def test_custom_config_is_private_and_links_are_rejected(self):
        self.write({"strava": {"client_secret": "synthetic-secret"}})
        self.local.chmod(0o644)
        self.load()
        self.assertEqual(stat.S_IMODE(self.local.stat().st_mode), 0o600)
        target = self.root / "original.json"
        self.local.rename(target)
        for hard_link in (False, True):
            with self.subTest(hard_link=hard_link):
                if hard_link:
                    os.link(target, self.local)
                else:
                    self.local.symlink_to(target)
                with self.assertRaisesRegex(ValueError, "普通文件|链接"):
                    self.load()
                self.local.unlink()
        self.local.symlink_to(self.root / "missing.json")
        with self.assertRaisesRegex(ValueError, "链接"):
            self.load()

    def test_invalid_config_cannot_be_replaced_by_token_generation(self):
        self.local.write_text('[]')
        with self.assertRaises(ValueError):
            self.ensure_token()
        self.assertEqual(self.local.read_text(), '[]')

    def test_webhook_token_is_generated_once_in_the_shared_custom_file(self):
        self.write({"strava": {"client_id": "test-client"}, "webhook": {"hooks": []}})
        first = self.ensure_token()
        self.assertTrue(first)
        self.assertEqual(self.ensure_token(), first)
        data = json.loads(self.local.read_text())
        self.assertEqual(data["strava"]["client_id"], "test-client")
        self.assertEqual(data["webhook"]["verify_token"], first)
        self.assertEqual(stat.S_IMODE(self.local.stat().st_mode), 0o600)

    def test_shell_exports_preserve_literal_values_without_execution(self):
        shutil.copy2(ROOT / "project_config.py", self.root)
        value = "quote' $(touch unexpected) `touch unexpected`\nsecond line"
        self.write({"strava": {"client_secret": value}})
        result = subprocess.run(["bash", "-c", 'set -e; settings=$(python3 project_config.py shell); '
                                 'eval "$settings"; printf "%s" "$CLIENT_SECRET"'],
                                cwd=self.root, env={}, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, value)
        self.assertFalse((self.root / "unexpected").exists())

    def test_template_text_is_loaded_from_partial_custom_config(self):
        from weather_template import load_template
        self.write({"weather": {"template": "本次天气 {t_min:.1f}°C"}})
        with patch("weather_template.ROOT", self.root):
            self.assertEqual(load_template().render({"t_min": 12.34}), "本次天气 12.3°C")

    def test_deployment_units_use_the_same_config_as_the_receiver(self):
        from project_config import render_units
        shutil.copytree(ROOT / "webhook/systemd", self.root / "webhook/systemd")
        self.write({"webhook": {"listen_port": 9876}, "ngrok": {
            "domain": "example.invalid", "executable": "/opt/my tools/ngrok", "config_file": "/private/ngrok.yml"}})
        target = self.root / "units"
        render_units(self.root, target, environ={})
        tunnel = (target / "strava-webhook-tunnel.service").read_text()
        self.assertIn('"/opt/my tools/ngrok"', tunnel)
        self.assertIn('"127.0.0.1:9876"', tunnel)
        self.assertIn('"/private/ngrok.yml"', tunnel)
        self.assertIn("https://example.invalid", tunnel)

    def test_backfill_driver_uses_configured_round_limit_and_retry_delay(self):
        for name in ("backfill_all.sh", "strava_common.sh", "project_config.py"):
            shutil.copy2(ROOT / name, self.root)
        self.write({"backfill": {"max_rounds": 2, "rate_retry_seconds": 5}})
        entry = self.root / "update_strava_weather.sh"
        entry.write_text('#!/bin/bash\necho called >> calls\nexit 1\n')
        entry.chmod(0o755)
        (self.root / "bin").mkdir()
        sleeper = self.root / "bin/sleep"
        sleeper.write_text('#!/bin/bash\necho "$1" >> sleeps\n')
        sleeper.chmod(0o755)
        result = subprocess.run(["bash", "backfill_all.sh"], cwd=self.root,
                                env={"PATH": f'{self.root / "bin"}:{os.environ["PATH"]}'},
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["called", "called"])
        self.assertEqual((self.root / "sleeps").read_text().splitlines(), ["5", "5"])

    def test_legacy_migration_preserves_credentials_hooks_and_existing_overrides(self):
        (self.root / "strava_config.sh").write_text(
            'CLIENT_ID="legacy-client"\nCLIENT_SECRET="synthetic-secret"\nREORDER_SUMMITBAG=0\n'
            'WEATHER_TEMPLATE_FILE="weather.custom.txt"\n')
        (self.root / "weather.custom.txt").write_text("天气 {t_min:.1f}°C")
        hook = self.root / "webhook"
        hook.mkdir()
        (hook / "config.sh").write_text('NGROK_DOMAIN="example.invalid"\n')
        legacy = {"verify_token": "legacy-token", "sync_delay_seconds": 120,
                  "hooks": [{"name": "test", "command": ["/path/to/script"]}]}
        (hook / "receiver_config.json").write_text(json.dumps(legacy))
        self.write({"webhook": {"sync_delay_seconds": 0}})
        self.migrate()
        config = self.load()
        self.assertEqual(config["strava"]["client_secret"], "synthetic-secret")
        self.assertEqual(config["webhook"]["verify_token"], "legacy-token")
        self.assertEqual(config["webhook"]["sync_delay_seconds"], 0)
        self.assertEqual(config["webhook"]["hooks"], legacy["hooks"])
        self.assertEqual(config["ngrok"]["domain"], "example.invalid")
        self.assertFalse(config["weather"]["reorder_summitbag"])
        self.assertEqual(config["weather"]["template"], "天气 {t_min:.1f}°C")
        self.assertEqual(config["weather"]["template_file"], "")
        self.assertTrue((self.root / "strava_config.sh").exists())
        before = self.local.read_text()
        self.migrate()
        self.assertEqual(self.local.read_text(), before)

    def test_migration_can_resolve_the_old_bundled_template(self):
        (self.root / "strava_config.sh").write_text('WEATHER_TEMPLATE_FILE="templates/weather.default.txt"\n')
        self.migrate()
        self.assertIn("Not Powered By www.gearaut.com 🍚", self.load()["weather"]["template"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
