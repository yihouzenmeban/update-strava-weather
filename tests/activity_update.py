#!/usr/bin/env python3
"""Exercise activity field calculation without HTTP, files or environment settings."""

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from activity_update import IndoorActivity, build_updates
from weather_template import WeatherTemplate


class ActivityUpdateTests(unittest.TestCase):
    def setUp(self):
        self.template = WeatherTemplate("天气 {t_min:.1f}°C")
        self.title = {"enabled": True, "template": "{title} {emoji}"}
        self.activity = {"id": 123, "name": "晨跑", "description": "手写备注", "sport_type": "Run"}

    def build(self, **options):
        return build_updates(self.activity, template=self.template, title_config=self.title, **options)

    def test_weather_peaks_map_and_commute_share_minimal_payload(self):
        original = copy.deepcopy(self.activity)
        updates = self.build(emoji="🌞", description="天气 20.0°C", peak_line="⛰️ 新峰 (500 m)",
                             map_style="pace", commute_fix=True)
        self.assertEqual(updates, {
            "name": "晨跑 🌞", "description": "手写备注\n⛰️ 新峰 (500 m)\n天气 20.0°C",
            "commute": True, "hide_from_home": True, "selected_polyline_style": "pace",
        })
        self.assertEqual(self.activity, original)

    def test_reorder_preserves_title_weather_and_surrounding_notes(self):
        self.activity.update(name="自定义标题 🏔️", description=(
            "手写备注\n天气 10.0°C\n天气后的备注\n⛰️ 旧峰 (100 m)"))
        self.assertEqual(self.build(reorder_only=True, peak_line="⛰️ 新峰 (500 m)"), {
            "description": "手写备注\n⛰️ 新峰 (500 m)\n天气 10.0°C\n天气后的备注\n",
        })

    def test_repeated_weather_and_peaks_produce_no_changes(self):
        self.activity.update(self.build(emoji="🌞", description="天气 20.0°C", peak_line="⛰️ 新峰 (500 m)"))
        self.assertEqual(self.build(emoji="🌞", description="天气 20.0°C", peak_line="⛰️ 新峰 (500 m)"), {})

    def test_map_style_can_be_only_change_in_reorder_mode(self):
        self.assertEqual(self.build(reorder_only=True, map_style="power"), {"selected_polyline_style": "power"})
        self.assertEqual(self.build(map_style="power"), {})

    def test_invalid_map_style_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "MAP_STYLE"):
            self.build(emoji="🌞", map_style="invalid style")

    def test_title_can_be_disabled(self):
        self.title["enabled"] = False
        self.assertEqual(self.build(emoji="🌞", description="天气 20.0°C"), {
            "description": "手写备注\n天气 20.0°C",
        })

    def test_indoor_activity_requires_explicit_force(self):
        for indoor in ({"trainer": True}, {"sport_type": "VirtualRide"}):
            with self.subTest(indoor=indoor):
                self.activity = dict(id=123, name="室内", description="备注", **indoor)
                with self.assertRaises(IndoorActivity):
                    self.build(emoji="🌞")
                self.assertEqual(self.build(emoji="🌞", force=True), {"name": "室内 🌞"})


if __name__ == "__main__":
    unittest.main()
