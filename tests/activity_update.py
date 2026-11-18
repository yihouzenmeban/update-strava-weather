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
            "description": "手写备注\n⛰️ 新峰 (500 m)\n天气 10.0°C\n天气后的备注",
        })

    def test_reorder_strips_trailing_blank_line_from_gearaut_block(self):
        # gearaut 天气块自带结尾换行；同步回传时清掉，描述末尾不留空行。
        gearaut_desc = (
            "温度: 1.46°C - 1.86°C  体感温度: -3.10°C - -2.50°C\n"
            "湿度: 29% - 30%  风速: 12.86 km/h - 11.75 km/h\n"
            "Powered By www.gearaut.com\n")
        self.activity.update(description=gearaut_desc)
        self.assertEqual(self.build(reorder_only=True), {
            "description": gearaut_desc.rstrip("\n"),
        })
        # 已干净的描述不再产生改动。
        self.activity["description"] = gearaut_desc.rstrip("\n")
        self.assertEqual(self.build(reorder_only=True), {})

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

    def test_trailrun_chinese_time_title_translates_to_english(self):
        cases = {
            "越野跑（上午） 🌞": "Morning Trail Run 🌞",
            "越野跑（下午） 🌧️": "Afternoon Trail Run 🌧️",
            "越野跑（傍晚） 🌞": "Evening Trail Run 🌞",
            "越野跑（凌晨） 🌙": "Early Morning Trail Run 🌙",
            "越野跑（中午） 🌞": "Midday Trail Run 🌞",
            "越野跑（晚上） 🌙": "Evening Trail Run 🌙",
            "越野跑（夜间） 🌙": "Night Trail Run 🌙",
            "越野跑（深夜） 🌙": "Late Night Trail Run 🌙",
            "越野跑(上午) 🌞": "Morning Trail Run 🌞",
            "越野跑（上午）": "Morning Trail Run",
        }
        for old, new in cases.items():
            with self.subTest(old=old):
                self.activity.update(name=old, sport_type="TrailRun")
                # 天气已处理时走 reorder_only 分支：只改标题，不动描述
                self.assertEqual(self.build(reorder_only=True), {"name": new})

    def test_trailrun_title_translation_applies_with_weather_write(self):
        self.activity.update(name="越野跑（上午）", sport_type="TrailRun")
        self.assertEqual(
            self.build(emoji="🌞", description="天气 20.0°C"),
            {"name": "Morning Trail Run 🌞", "description": "手写备注\n天气 20.0°C"})

    def test_trailrun_title_translation_skips_unknown_time_and_other_sports(self):
        for name, sport_type in (("越野跑（周末） 🌞", "TrailRun"),
                                 ("越野跑（上午） 🌞", "Run"),
                                 ("越野跑（上午） 🌞", "Hike"),
                                 ("午后远足 🌞", "TrailRun")):
            with self.subTest(name=name, sport_type=sport_type):
                self.activity.update(name=name, sport_type=sport_type)
                self.assertEqual(self.build(reorder_only=True), {})


if __name__ == "__main__":
    unittest.main()
