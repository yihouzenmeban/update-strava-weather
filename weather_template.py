"""Render plain-text weather templates and locate their blocks in descriptions."""

from pathlib import Path
import re
from string import Formatter


ROOT = Path(__file__).resolve().parent
NUMERIC_FIELDS = frozenset(("t_min", "t_max", "at_min", "at_max", "h_min", "h_max", "w_min", "w_max"))
WEATHER_EMOJIS = ("⛈️", "❄️", "🌧️", "🌫️", "🌙", "☁️", "⛅", "🌞", "💨", "🥵", "🥶")
NUMBER = r"-?\d+(?:\.\d+)?"
EMOJI_PATTERN = "(?:" + "|".join(re.escape(emoji) for emoji in WEATHER_EMOJIS) + ")+"
# Legacy layouts have a known start and signature; never span arbitrary notes.
LEGACY_BLOCK = re.compile(
    r"^温度:[^\r\n]*\r?\n(?:湿度:[^\r\n]*\r?\n)?"
    r"(?:Not Powered By www\.gearaut\.com [🍚👻]|Powered By "
    r"(?:www\.gearaut\.com|QWeather \(www\.qweather\.com\)))[ \t]*\r?$", re.M,
)


class WeatherTemplate:
    def __init__(self, source):
        source = source.replace("\r\n", "\n").strip()
        try:
            parts = list(Formatter().parse(source))
        except ValueError as error:
            raise ValueError(f"天气模板括号格式错误：{error}") from None
        if not source or not any(literal.strip() for literal, *_ in parts):
            raise ValueError("天气模板不能为空，且须包含固定文案以识别天气块")
        pattern = []
        self.parts = []
        for literal, field, spec, conversion in parts:
            pattern.append(r"\r?\n".join(re.escape(line) for line in literal.split("\n")))
            if field is not None:
                if field not in NUMERIC_FIELDS and field != "emoji":
                    raise ValueError(f"天气模板包含未知字段：{field}")
                if conversion is not None or (field == "emoji" and spec) or (
                        field in NUMERIC_FIELDS and spec and not re.fullmatch(r"\.[0-6]f", spec)):
                    raise ValueError(f"天气模板字段 {field} 的格式无效，数值支持 .0f 至 .6f")
                pattern.append(EMOJI_PATTERN if field == "emoji" else NUMBER)
            self.parts.append((literal, field, spec))
        self.pattern = re.compile("^" + "".join(pattern) + r"\r?$", re.M)

    def render(self, values):
        output = []
        for literal, field, spec in self.parts:
            output.append(literal)
            if field is not None:
                output.append(values[field] if field == "emoji" else format(values[field], spec or ".2f"))
        return "".join(output)


def load_template(path=None):
    selected = Path(path) if path else Path("templates/weather.default.txt")
    if not selected.is_absolute():
        selected = ROOT / selected
    try:
        source = selected.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ValueError(f"无法读取 UTF-8 天气模板：{selected}") from None
    return WeatherTemplate(source)


def weather_spans(description, template=None):
    template = template if template is not None else load_template()
    spans = sorted({match.span() for pattern in (LEGACY_BLOCK, template.pattern)
                    for match in pattern.finditer(description or "")})
    merged = []
    for start, end in spans:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def has_weather(description, template=None):
    description = description or ""
    return "www.gearaut.com" in description or bool(weather_spans(description, template))


def strip_weather(description, template=None):
    description = description or ""
    for start, end in reversed(weather_spans(description, template)):
        # Consume only the removed block's newline, preserving surrounding text.
        if description[end:end + 2] == "\r\n":
            end += 2
        elif description[end:end + 1] == "\n":
            end += 1
        description = description[:start] + description[end:]
    if "www.gearaut.com" in description:
        raise ValueError("检测到 www.gearaut.com，但无法定位旧天气块；请使用原模板或手动清理旧天气后重试")
    return description.strip()


def reorder_summitbag(description, template=None, enabled=True):
    """Place mountain data after handwritten prefixes and before the weather."""
    if not enabled or not description:
        return description
    spans = weather_spans(description, template)
    if not spans:
        return description
    newline = "\r\n" if "\r\n" in description else "\n"
    remaining, mountains = [], []
    offset, insertion = 0, 0
    prefix_mountain, needs_move = False, False
    for line in description.splitlines(keepends=True):
        end = offset + len(line)
        if offset == spans[0][0]:
            insertion = len(remaining)
        text = line.strip()
        elevation = re.search(r"[（(]\s*\d[\d,.]*\s*(?:m|米)\s*[）)]", text)
        summit_link = re.fullmatch(r"(?:🌐\s*)?(?:https?://)?(?:www\.)?summitbag\.com(?:[/?#]\S*)?", text)
        is_mountain = bool(summit_link or (elevation and (text.startswith(("⛰", "🏔")) or "summitbag.com" in text)))
        inside_weather = any(offset < stop and end > start for start, stop in spans)
        if is_mountain and not inside_weather:
            mountains.append(line if line.endswith("\n") else line + newline)
            if offset >= spans[0][1]:
                needs_move = True
            else:
                prefix_mountain = True
        else:
            if offset < spans[0][0] and text and prefix_mountain:
                needs_move = True
            remaining.append(line)
        offset = end
    if not needs_move:
        return description
    result = "".join(remaining[:insertion] + mountains + remaining[insertion:]).rstrip("\r\n")
    return result + (newline if description.endswith("\n") else "")
