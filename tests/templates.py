#!/usr/bin/env python3
"""Template rendering and weather recognition, without external requests."""

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from weather_template import WeatherTemplate, has_weather, load_template, reorder_summitbag, strip_weather, weather_spans


VALUES = dict(t_min=22, t_max=26, at_min=32, at_max=36,
              h_min=70, h_max=90, w_min=5, w_max=9, emoji="🌧️🥵")
DEFAULT = ("温度: 22.00°C - 26.00°C  体感温度: 32.00°C - 36.00°C\n"
           "湿度: 70.00% - 90.00%  风速: 5.00 km/h - 9.00 km/h\n"
           "Not Powered By www.gearaut.com 🍚")
MOUNTAINS = "⛰️ 馒头峪 (767 m) • ⛰️ 萝芭地北尖 (1,146 m) • ⛰️ Yangtai Shan (1,276 m) | 🌐 summitbag.com"


class TemplateTests(unittest.TestCase):
    def test_default_output_is_unchanged(self):
        self.assertEqual(load_template().render(VALUES), DEFAULT)

    def test_custom_precision_emoji_and_escaped_braces(self):
        template = WeatherTemplate("{{天气}} {emoji}\n气温 {t_min:.1f}～{t_max:.0f}°C\n风 {w_max} km/h\n")
        self.assertEqual(template.render(VALUES), "{天气} 🌧️🥵\n气温 22.0～26°C\n风 9.00 km/h")

    def test_invalid_templates_fail_with_readable_errors(self):
        for source in ("", "   ", "{unknown}", "气温 {t_min.real}", "气温 {t_min[0]}",
                       "气温 {t_min!r}", "气温 {t_min:{t_max}}", "气温 {t_min:.1000f}",
                       "天气 {emoji:.2f}", "气温 {t_min", "{t_min}"):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "模板"):
                WeatherTemplate(source)

    def test_missing_or_invalid_utf8_file_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "weather.txt"
            with self.assertRaisesRegex(ValueError, "模板"):
                load_template(path)
            path.write_bytes(b"\xff")
            with self.assertRaisesRegex(ValueError, "模板"):
                load_template(path)

    def test_domain_or_current_template_recognizes_existing_weather(self):
        template = WeatherTemplate("天气 {emoji}\n气温 {t_min:.1f}～{t_max:.1f}°C")
        self.assertTrue(has_weather("旧格式 www.gearaut.com 已添加", template))
        for newline in ("\n", "\r\n"):
            description = newline.join(("原备注", "天气 🌙💨🥶", "气温 -2.5～0.0°C", "后记"))
            self.assertTrue(has_weather(description, template))
            self.assertEqual(strip_weather(description, template).splitlines(), ["原备注", "后记"])
        self.assertFalse(has_weather("原备注\n气温 20°C", template))

    def test_template_literals_are_matched_literally_and_on_whole_lines(self):
        template = WeatherTemplate("天气[自定义].* {t_min:.1f}°C")
        self.assertTrue(has_weather("备注\n天气[自定义].* 2.0°C\n后记", template))
        self.assertFalse(has_weather("天气自定义 anything 2.0°C", template))
        self.assertFalse(has_weather("引用：天气[自定义].* 2.0°C", template))

    def test_legacy_and_custom_blocks_are_removed_without_eating_notes(self):
        template = WeatherTemplate("气温 {t_min:.1f}～{t_max:.1f}°C")
        for signature in ("Powered By www.gearaut.com", "Not Powered By www.gearaut.com 👻",
                          "Powered By QWeather (www.qweather.com)"):
            legacy = "温度: 10°C - 12°C\n湿度: 60% - 70%\n" + signature
            description = "原备注\n" + legacy + "\n后记\n气温 1.0～2.0°C\nhttps://summitbag.com/example"
            self.assertEqual(strip_weather(description, template), "原备注\n后记\nhttps://summitbag.com/example")

    def test_unlocatable_domain_block_is_not_deleted_or_duplicated(self):
        with self.assertRaisesRegex(ValueError, "无法定位"):
            strip_weather("重要备注\n旧天气布局 www.gearaut.com\n后记", load_template())

    def test_spans_delimit_only_the_weather_for_reordering(self):
        template = WeatherTemplate("气温 {t_min}°C")
        description = "备注\n气温 20.00°C\n后记\nhttps://summitbag.com/example"
        spans = weather_spans(description, template)
        self.assertEqual(len(spans), 1)
        start, end = spans[0]
        self.assertEqual(description[start:end], "气温 20.00°C")

    def test_mountains_move_before_legacy_weather_without_changing_values(self):
        weather = ("温度: 13.58°C - 21.48°C 体感温度: 13.60°C - 22.50°C\n"
                   "湿度: 89% - 69% 风速: 2.98 km/h - 3.95 km/h\nPowered By www.gearaut.com")
        self.assertEqual(reorder_summitbag(weather + "\n\n" + MOUNTAINS), MOUNTAINS + "\n" + weather)

    def test_mountain_lines_and_domain_lines_move_together_preserving_notes_and_line_endings(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=newline):
                weather = DEFAULT.replace("\n", newline)
                mountains = newline.join(("⛰️ 馒头峪 (767 m)", "🏔️ Yangtai Shan (1,276 m)", "🌐 summitbag.com"))
                description = newline.join(("原备注", weather, "重要后记", mountains, "最后的备注"))
                expected = newline.join(("原备注", mountains, weather, "重要后记", "最后的备注"))
                self.assertEqual(reorder_summitbag(description), expected)
                self.assertEqual(reorder_summitbag(expected), expected)

    def test_reorder_can_be_disabled_and_does_not_touch_unrelated_descriptions(self):
        description = DEFAULT + "\n" + MOUNTAINS
        self.assertEqual(reorder_summitbag(description, enabled=False), description)
        for source in ("", "原备注\n" + MOUNTAINS, DEFAULT + "\n后记", MOUNTAINS + "\n" + DEFAULT):
            with self.subTest(source=source):
                self.assertEqual(reorder_summitbag(source), source)

    def test_reorder_supports_custom_templates_and_keeps_weather_lines_in_place(self):
        template = WeatherTemplate("⛰️ 跑山天气\n气温 {t_min:.1f}～{t_max:.1f}°C")
        weather = "⛰️ 跑山天气\n气温 1.0～2.0°C"
        self.assertEqual(reorder_summitbag("原备注\n" + weather + "\n" + MOUNTAINS, template),
                         "原备注\n" + MOUNTAINS + "\n" + weather)


if __name__ == "__main__":
    unittest.main()
