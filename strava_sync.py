#!/usr/bin/env python3
"""Discover activity batches and durably synchronize missing weather."""

import argparse
from bisect import bisect_left, bisect_right
import datetime as dt
import fcntl
import gzip
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parent
FIELDS = ("temperature_2m", "apparent_temperature", "relative_humidity_2m", "wind_speed_10m")
VARIABLES = ",".join(FIELDS + ("weather_code",))
NUMBER = r"-?\d+(?:\.\d+)?"
WEATHER_BLOCK = re.compile(
    rf"^温度: {NUMBER}°C - {NUMBER}°C(?:  体感温度: {NUMBER}°C - {NUMBER}°C)?\r?\n"
    rf"湿度: {NUMBER}% - {NUMBER}%  风速: {NUMBER} km/h - {NUMBER} km/h\r?\n"
    r"(?:Not Powered By www\.gearaut\.com [🍚👻]|Powered By (?:www\.gearaut\.com|QWeather \(www\.qweather\.com\)))(?:\r?$)", re.M,
)


class SyncError(Exception):
    pass


class StopBatchError(SyncError):
    pass


def has_weather(description):
    return bool(WEATHER_BLOCK.search(description or ""))


def private_open(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        os.close(fd)
        raise SyncError("处理记录和锁必须为当前用户拥有的普通文件，不能使用链接")
    os.fchmod(fd, 0o600)
    return fd


def load_state(path):
    try:
        fd = private_open(path, os.O_RDONLY)
    except FileNotFoundError:
        return {"version": 2, "activities": {}, "pending": {}, "last_scan": None, "scan_from": None}
    with os.fdopen(fd, encoding="utf-8") as source:
        data = json.load(source)
    if not isinstance(data, dict):
        raise SyncError("处理记录格式错误，请检查 .strava_weather_state.json")
    if type(data.get("version")) is not int or data["version"] not in (1, 2):
        raise SyncError("处理记录版本无效，请检查 .strava_weather_state.json")
    activities = data.get("activities")
    if not isinstance(activities, dict) or any(
            not re.fullmatch(r"[1-9][0-9]*", key) or status not in ("updated", "existing", "indoor")
            for key, status in activities.items()):
        raise SyncError("处理记录格式错误，请检查 .strava_weather_state.json")
    if data["version"] == 1:
        return {"version": 2, "activities": activities, "pending": {}, "last_scan": None, "scan_from": None}
    pending = data.get("pending")
    if (not isinstance(pending, dict) or "last_scan" not in data
            or any(not re.fullmatch(r"[1-9][0-9]*", key) or type(force) is not bool for key, force in pending.items())
            or any(value is not None and (type(value) is not int or value < 0)
                   for value in (data["last_scan"], data.get("scan_from")))):
        raise SyncError("处理记录格式错误，请检查 .strava_weather_state.json")
    return {"version": 2, "activities": activities, "pending": pending,
            "last_scan": data["last_scan"], "scan_from": data.get("scan_from")}


def save_state(path, state):
    fd, temporary = tempfile.mkstemp(prefix=".strava_weather_state.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(state, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def invoke(command, activity_id=None, extra=None, arguments=()):
    environment = dict(os.environ, STRAVA_NONINTERACTIVE="1")
    if activity_id is not None:
        environment["ACTIVITY_ID"] = str(activity_id)
    if extra:
        environment.update(extra)
    return subprocess.run(["bash", str(ROOT / "update_strava_weather.sh"), command, *arguments],
                          env=environment, text=True, capture_output=True)


def script_error(result, fallback):
    message = result.stderr.strip() or fallback
    if any(marker in message for marker in ("HTTP 401", "HTTP 403", "HTTP 429", "完成授权")):
        return StopBatchError(message)
    return SyncError(message)


def read_strava(command, activity_id=None, allow_missing=False, extra=None, arguments=()):
    result = invoke(command, activity_id, extra, arguments)
    if result.returncode:
        if allow_missing and "HTTP 404" in result.stderr:
            return {}
        raise script_error(result, f"Strava {command} 读取失败")
    return json.loads(result.stdout)


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def interval_samples(times, values, start, end):
    if (not isinstance(times, (list, tuple)) or not isinstance(values, (list, tuple))
            or not times or len(times) != len(values) or any(not numeric(time) for time in times)
            or any(left >= right for left, right in zip(times, times[1:]))
            or start > end or start < times[0] or end > times[-1]):
        raise SyncError("天气采样未覆盖完整活动时段或时间序列无效")

    def interpolate(time):
        index = bisect_left(times, time)
        if times[index] == time:
            value = values[index]
            if not numeric(value):
                raise SyncError("活动时段内天气数据缺失")
            return value
        left, right = values[index - 1:index + 1]
        if not numeric(left) or not numeric(right):
            raise SyncError("活动边界天气数据缺失")
        return left + (right - left) * (time - times[index - 1]) / (times[index] - times[index - 1])

    samples = values[bisect_left(times, start):bisect_right(times, end)]
    if any(not numeric(value) for value in samples):
        raise SyncError("活动时段内天气数据缺失")
    return list(samples) + [interpolate(start), interpolate(end)]


def interval_extrema(times, values, start, end):
    samples = interval_samples(times, values, start, end)
    return min(samples), max(samples)


def steadman_apparent_temperature(temperature, humidity, wind_kmh):
    if (any(not numeric(value) for value in (temperature, humidity, wind_kmh))
            or not -100 <= temperature <= 70 or not 0 <= humidity <= 100 or wind_kmh < 0):
        raise SyncError("计算体感温度所需的温度、湿度或风速无效")
    # Steadman/BOM 无辐射近似：水汽压用 hPa，10 米风速由 km/h 换算为 m/s。
    vapor_pressure = humidity / 100 * 6.105 * math.exp(17.27 * temperature / (237.7 + temperature))
    return temperature + 0.33 * vapor_pressure - 0.70 * (wind_kmh / 3.6) - 4.00


def weather_url(latitude, longitude, start, end, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    parameters = {"latitude": latitude, "longitude": longitude, "timezone": "GMT",
                  "timeformat": "unixtime", "temperature_unit": "celsius", "wind_speed_unit": "kmh"}
    if start < (now - dt.timedelta(days=5)).timestamp():
        key, endpoint = "hourly", "https://archive-api.open-meteo.com/v1/archive"
        parameters.update(start_date=dt.datetime.fromtimestamp(start, dt.timezone.utc).date().isoformat(),
                          end_date=dt.datetime.fromtimestamp(math.ceil(end / 3600) * 3600, dt.timezone.utc).date().isoformat())
    else:
        key, endpoint = "minutely_15", "https://api.open-meteo.com/v1/forecast"
        parameters.update(start_minutely_15=dt.datetime.fromtimestamp(math.floor(start / 900) * 900, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M"),
                          end_minutely_15=dt.datetime.fromtimestamp(math.ceil(end / 900) * 900, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M"))
    parameters[key] = VARIABLES
    return endpoint + "?" + urlencode(parameters), key


def qweather_config():
    host = os.environ.get("QWEATHER_API_HOST", "").strip()
    if not re.fullmatch(r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+qweatherapi\.com", host):
        raise SyncError("请配置 QWEATHER_API_HOST：控制台分配的 qweatherapi.com 域名，不含 https:// 或路径")
    jwt = os.environ.get("QWEATHER_JWT", "")
    key = os.environ.get("QWEATHER_API_KEY", "")
    credential = jwt or key
    if not credential or any(ord(character) < 33 or ord(character) > 126 for character in credential):
        raise SyncError("请通过配置或环境变量提供有效的 QWEATHER_API_KEY 或 QWEATHER_JWT")
    return host, ("Authorization", "Bearer " + jwt) if jwt else ("X-QW-Api-Key", key)


def qweather_request(path, parameters):
    host, (header, credential) = qweather_config()
    request = urllib.request.Request("https://" + host + path + "?" + urlencode(parameters))
    # 认证信息不随 HTTP 重定向转发，也不进入 URL 或进程参数。
    request.add_unredirected_header(header, credential)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
        if isinstance(body, bytes) and body.startswith(b"\x1f\x8b"):
            body = gzip.decompress(body)
        payload = json.loads(body)
    except urllib.error.HTTPError as error:
        error_type = StopBatchError if error.code in (401, 402, 403, 429) else SyncError
        raise error_type(f"和风天气 HTTP {error.code}，请求失败，待处理记录已保留") from None
    except (urllib.error.URLError, OSError, ValueError, EOFError):
        raise SyncError("和风天气请求失败或响应无效，待处理记录已保留") from None
    if not isinstance(payload, dict):
        raise SyncError("和风天气响应格式无效，待处理记录已保留")
    code = str(payload.get("code", ""))
    if code != "200":
        error_type = StopBatchError if code in ("401", "402", "403", "429") else SyncError
        raise error_type("和风天气返回错误，请检查凭据、接口权限、配额及日期，待处理记录已保留")
    return payload


def qweather_code(icon):
    try:
        code = int(icon)
    except (TypeError, ValueError):
        raise SyncError("和风天气状况代码无效") from None
    if code in (100, 150):
        return 0
    if code in (101, 102, 103, 151, 152, 153):
        return 2
    if code == 104:
        return 3
    if code in (302, 303, 304):
        return 95
    if 400 <= code <= 499:
        return 71
    if 300 <= code <= 399:
        return 61
    if 500 <= code <= 515:
        return 45
    raise SyncError("和风天气状况代码无效")


def qweather_series(location, start, end, now=None):
    latitude, longitude = location
    city = qweather_request("/geo/v2/city/lookup", {"location": f"{longitude:.2f},{latitude:.2f}", "number": 1})
    try:
        city = city["location"][0]
        city_id = city["id"]
        zone = ZoneInfo(city["tz"])
        if not isinstance(city_id, str) or not city_id:
            raise ValueError
    except (KeyError, IndexError, TypeError, ValueError, ZoneInfoNotFoundError):
        raise SyncError("和风天气未返回有效城市 ID 或时区") from None
    today = (now or dt.datetime.now(dt.timezone.utc)).astimezone(zone).date()
    first = dt.datetime.fromtimestamp(start, zone).date()
    # 末尾插值需要下一整点，可能需要额外查询一天。
    local_end = dt.datetime.fromtimestamp(end, zone)
    next_hour = local_end.replace(minute=0, second=0, microsecond=0)
    if next_hour.timestamp() < end:
        next_hour += dt.timedelta(hours=1)
    last = next_hour.date()
    if first < today - dt.timedelta(days=10):
        raise SyncError("和风天气仅支持最近 10 天历史数据，请改用 --weather-provider open-meteo 补扫")
    if last >= today:
        raise SyncError("和风天气历史数据不含今天，活动保留待处理，数据可用后重试")
    series = {field: [] for field in ("time", "temperature_2m", "relative_humidity_2m", "wind_speed_10m", "weather_code")}
    day = first
    while day <= last:
        payload = qweather_request("/v7/historical/weather", {"location": city_id, "date": day.strftime("%Y%m%d"), "unit": "m"})
        rows = payload.get("weatherHourly")
        if not isinstance(rows, list) or not rows:
            raise SyncError("和风天气缺少逐小时历史数据，待处理记录已保留")
        for row in rows:
            try:
                time = dt.datetime.fromisoformat(row["time"])
                if time.tzinfo is None:
                    time = time.replace(tzinfo=zone)
                temperature, humidity, wind = (float(row[field]) for field in ("temp", "humidity", "windSpeed"))
                if (any(not numeric(value) for value in (temperature, humidity, wind))
                        or not -100 <= temperature <= 70 or not 0 <= humidity <= 100 or wind < 0):
                    raise ValueError
                code = qweather_code(row["icon"])
            except (KeyError, TypeError, ValueError, OverflowError):
                raise SyncError("和风天气逐小时数据无效，待处理记录已保留") from None
            for field, value in zip(series, (time.timestamp(), temperature, humidity, wind, code)):
                series[field].append(value)
        day += dt.timedelta(days=1)
    times = series["time"]
    if any(right - left != 3600 for left, right in zip(times, times[1:])):
        raise SyncError("和风天气逐小时数据不连续，待处理记录已保留")
    return series


def coordinates(value):
    if isinstance(value, (list, tuple)) and len(value) == 2:
        latitude, longitude = value
        if numeric(latitude) and numeric(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180:
            return latitude, longitude
    return None


def weather_annotation(activity, location, provider="open-meteo"):
    try:
        start_date = dt.datetime.fromisoformat(activity["start_date"].replace("Z", "+00:00"))
        local_start = dt.datetime.fromisoformat(activity["start_date_local"].replace("Z", "+00:00")).replace(tzinfo=None)
        duration = activity["moving_time"]
        if start_date.tzinfo is None or not numeric(duration) or duration < 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise SyncError("活动缺少有效的 start_date、start_date_local 或 moving_time") from None
    start, end = start_date.timestamp(), start_date.timestamp() + duration
    if provider == "qweather":
        series = qweather_series(location, start, end)
    else:
        url, key = weather_url(*location, start, end)
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403, 429):
                raise StopBatchError(f"Open-Meteo HTTP {error.code}，停止本轮处理，待处理记录已保留") from None
            raise SyncError("Open-Meteo 请求失败，未记录成功，下次可重试") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise SyncError("Open-Meteo 请求失败，未记录成功，下次可重试") from None
        series = payload.get(key, {}) if isinstance(payload, dict) else {}
    if not isinstance(series, dict):
        raise SyncError("Open-Meteo 未返回有效天气序列，未记录成功")
    times = series.get("time", [])
    fields = tuple(field for field in FIELDS if provider != "qweather" or field != "apparent_temperature")
    ranges = {field: interval_extrema(times, series.get(field, []), start, end) for field in fields}
    if provider == "qweather":
        # 先插值首尾的原始输入，再逐点计算，避免对非线性的 AT 直接插值。
        inputs = [interval_samples(times, series[field], start, end) for field in fields]
        apparent_samples = [steadman_apparent_temperature(*values) for values in zip(*inputs)]
        ranges["apparent_temperature"] = min(apparent_samples), max(apparent_samples)
    first, last = max(0, bisect_right(times, start) - 1), bisect_right(times, end)
    weather_codes = series.get("weather_code", [])
    if (not isinstance(weather_codes, list) or len(weather_codes) != len(times)
            or any(not numeric(code) for code in weather_codes[first:last])):
        raise SyncError("活动时段内 weather_code 缺失")
    codes = weather_codes[first:last]
    if any(code in (95, 96, 99) for code in codes):
        emoji = "⛈️"
    elif any(71 <= code <= 77 or code in (85, 86) for code in codes):
        emoji = "❄️"
    elif any(51 <= code <= 67 or 80 <= code <= 82 for code in codes):
        emoji = "🌧️"
    elif any(code in (45, 48) for code in codes):
        emoji = "🌫️"
    else:
        local_end = local_start + dt.timedelta(seconds=duration)
        night = duration < 12 * 3600 and all(time.hour >= 20 or time.hour < 5 for time in (local_start, local_end))
        emoji = "🌙" if night else "☁️" if 3 in codes else "⛅" if 2 in codes else "🌞"
    if ranges["wind_speed_10m"][1] > 30:
        emoji += "💨"
    apparent = ranges.get("apparent_temperature")
    if apparent and apparent[1] > 30:
        emoji += "🥵"
    if apparent and apparent[0] < 0:
        emoji += "🥶"
    temperature, humidity, wind = (ranges[field] for field in ("temperature_2m", "relative_humidity_2m", "wind_speed_10m"))
    feels_like = f"  体感温度: {apparent[0]:.2f}°C - {apparent[1]:.2f}°C" if apparent else ""
    description = (f"温度: {temperature[0]:.2f}°C - {temperature[1]:.2f}°C{feels_like}\n"
                   f"湿度: {humidity[0]:.2f}% - {humidity[1]:.2f}%  风速: {wind[0]:.2f} km/h - {wind[1]:.2f} km/h\n"
                   "Not Powered By www.gearaut.com 🍚")
    return emoji, description


def process_activity(args, activity_id, activities, force=False):
    if not force and activity_id in activities:
        print(f"⏭️ 活动 {activity_id} 已处理，跳过详情和天气查询")
        return None
    activity = read_strava("get", activity_id)
    if not isinstance(activity, dict) or str(activity.get("id")) != activity_id:
        raise SyncError("Strava 返回的活动与请求 ID 不一致")
    if activity.get("trainer") or str(activity.get("sport_type", "")).startswith("Virtual"):
        print(f"⏭️ 活动 {activity_id} 为室内活动，跳过")
        return "indoor"
    if not force and has_weather(activity.get("description")):
        print(f"⏭️ 活动 {activity_id} 已有完整天气块，跳过")
        return "existing"
    location = coordinates([args.latitude, args.longitude]) if args.latitude is not None else coordinates(activity.get("start_latlng"))
    if location is None:
        streams = read_strava("streams", activity_id, allow_missing=True)
        if not isinstance(streams, dict):
            raise SyncError("Strava 坐标流格式无效")
        stream = streams.get("latlng") or {}
        points = (stream.get("data") or []) if isinstance(stream, dict) else []
        if not isinstance(points, list):
            raise SyncError("Strava 坐标流格式无效")
        location = next((coordinates(point) for point in points if coordinates(point)), None)
    if location is None:
        raise SyncError(f"活动 {activity_id} 无有效 GPS 坐标，请提供 --latitude 和 --longitude 后重试")
    emoji, description = weather_annotation(activity, location, args.weather_provider)
    extra = {"WEATHER_EMOJI": emoji, "WEATHER_DESC": description, "WEATHER_EMOJI_FILE": "", "WEATHER_DESC_FILE": "",
             "YES": "1", "DRY_RUN": "1" if args.dry_run else "0",
             "STRAVA_SYNC_CHECK_WEATHER": "0" if force else "1"}
    result = invoke("show" if args.dry_run else "run", activity_id, extra)
    if result.returncode not in (0, 2, 3):
        raise script_error(result, "Strava 写入失败，未记录成功")
    print(result.stdout.strip())
    return {0: "updated", 2: "indoor", 3: "existing"}[result.returncode]


def enqueue(state, activity_id, force):
    if activity_id in state["pending"]:
        state["pending"][activity_id] = state["pending"][activity_id] or force
    elif force or activity_id not in state["activities"]:
        state["pending"][activity_id] = force


def discover(args, state, state_path, now):
    if args.activity_id is not None:
        enqueue(state, args.activity_id, args.force)
        if not args.dry_run and args.activity_id in state["pending"]:
            save_state(state_path, state)
        return [args.activity_id] if args.activity_id in state["pending"] else []

    incremental = not args.all and args.since is None
    before = min(now, args.until_timestamp) if args.until_timestamp is not None else now
    if args.all:
        after = 0
    elif args.since is not None:
        after = args.since_timestamp
    else:
        if state["last_scan"] is not None and state["last_scan"] > now:
            raise SyncError("上次扫描时间晚于当前时间，请检查系统时钟和处理记录")
        after = state["scan_from"]
        if after is None:
            after = max(0, (state["last_scan"] if state["last_scan"] is not None else now) - 7 * 86400)
        state["scan_from"] = after
        if not args.dry_run:
            save_state(state_path, state)
    if after >= before:
        raise SyncError("查询起点必须早于本轮查询上限")

    page, found = 1, {}
    while True:
        batch = read_strava("list", arguments=(str(args.page_size),), extra={
            "STRAVA_PAGE": str(page), "STRAVA_AFTER": str(max(0, after - 1)), "STRAVA_BEFORE": str(before),
        })
        if not isinstance(batch, list) or len(batch) > args.page_size:
            raise SyncError("Strava 活动列表格式无效")
        ids = []
        for activity in batch:
            activity_id = str(activity.get("id", "")) if isinstance(activity, dict) else ""
            if not re.fullmatch(r"[1-9][0-9]*", activity_id):
                raise SyncError("Strava 活动列表包含无效 ID，扫描进度未推进")
            ids.append(activity_id)
        if len(batch) == args.page_size and not any(activity_id not in found for activity_id in ids):
            raise SyncError("Strava 分页未推进，扫描进度未推进，请稍后重试")
        for activity_id in ids:
            found[activity_id] = None
            enqueue(state, activity_id, args.force)
        if ids and not args.dry_run:
            save_state(state_path, state)
        if len(batch) < args.page_size:
            break
        page += 1
    if incremental:
        state["last_scan"] = before
        state["scan_from"] = None
        if not args.dry_run:
            save_state(state_path, state)
    selected = list(state["pending"]) if incremental else [activity_id for activity_id in found if activity_id in state["pending"]]
    print(f"→ 扫描到 {len(found)} 条活动，本轮待处理 {len(selected)} 条")
    return selected


def parse_args():
    parser = argparse.ArgumentParser(description="批量增量同步 Strava 活动天气，失败活动保留到队列")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--activity-id", help="只处理指定活动，不查询列表")
    scope.add_argument("--all", action="store_true", help="分页补扫全部历史")
    scope.add_argument("--since", help="从此 UTC 日期开始补扫，格式 YYYY-MM-DD")
    parser.add_argument("--until", help="补扫截止 UTC 日期，包含当天；需与 --since 配合")
    parser.add_argument("--page-size", type=int, default=50, help="每页活动数，默认 50，范围 1–200")
    parser.add_argument("--force", action="store_true", help="重算查询范围内活动，室内活动仍跳过")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写活动、队列或扫描进度")
    parser.add_argument("--latitude", type=float, help="指定单条活动的纬度")
    parser.add_argument("--longitude", type=float, help="指定单条活动的经度")
    parser.add_argument("--weather-provider", choices=("open-meteo", "qweather"),
                        default=os.environ.get("WEATHER_PROVIDER", "open-meteo"), help="天气数据源，默认 open-meteo；可通过 WEATHER_PROVIDER 配置")
    args = parser.parse_args()
    if args.weather_provider not in ("open-meteo", "qweather"):
        parser.error("WEATHER_PROVIDER 必须为 open-meteo 或 qweather")
    if args.weather_provider == "qweather":
        try:
            qweather_config()
        except SyncError as error:
            parser.error(str(error))
    args.dry_run = args.dry_run or os.environ.get("DRY_RUN") == "1"
    if args.activity_id is not None and not re.fullmatch(r"[1-9][0-9]*", args.activity_id):
        parser.error("--activity-id 必须为正整数")
    if not 1 <= args.page_size <= 200:
        parser.error("--page-size 必须在 1–200 之间")
    if (args.latitude is None) != (args.longitude is None) or (
            args.latitude is not None and (args.activity_id is None or coordinates([args.latitude, args.longitude]) is None)):
        parser.error("经纬度必须与 --activity-id 一起使用，并同时提供有效的 --latitude 和 --longitude")
    if args.until is not None and args.since is None:
        parser.error("--until 需与 --since 一起使用")
    args.since_timestamp = args.until_timestamp = None
    try:
        if args.since is not None:
            since = dt.date.fromisoformat(args.since)
            until = dt.date.fromisoformat(args.until) if args.until is not None else None
            if until is not None and until < since:
                raise ValueError
            args.since_timestamp = int(dt.datetime.combine(since, dt.time.min, dt.timezone.utc).timestamp())
            if until is not None:
                args.until_timestamp = int(dt.datetime.combine(until + dt.timedelta(days=1), dt.time.min, dt.timezone.utc).timestamp())
            if args.since_timestamp >= dt.datetime.now(dt.timezone.utc).timestamp():
                raise ValueError
    except (ValueError, OverflowError):
        parser.error("日期范围无效：使用 YYYY-MM-DD，起点须早于当前时间，截止日期不得早于起点")
    return args


def main():
    args = parse_args()
    os.umask(0o077)
    try:
        fd = private_open(ROOT / ".strava_weather_state.lock", os.O_RDWR | os.O_CREAT)
        with os.fdopen(fd, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("⏭️ 已有 sync 正在运行，本次跳过")
                return 0
            state_path = ROOT / ".strava_weather_state.json"
            state = load_state(state_path)
            selected = discover(args, state, state_path, int(dt.datetime.now(dt.timezone.utc).timestamp()))
            completed, failures = 0, 0
            for activity_id in selected:
                try:
                    status = process_activity(args, activity_id, state["activities"], state["pending"][activity_id])
                except (SyncError, OSError, ValueError) as error:
                    failures += 1
                    print(f"❌ 活动 {activity_id}: {error}", file=sys.stderr)
                    if isinstance(error, StopBatchError):
                        break
                    continue
                completed += 1
                if not args.dry_run:
                    if status is not None:
                        state["activities"][activity_id] = status
                    state["pending"].pop(activity_id)
                    save_state(state_path, state)
            verb = "预览完成" if args.dry_run else "完成"
            print(f"→ 本轮{verb} {completed} 条，失败 {failures} 条，待处理队列 {len(state['pending'])} 条")
            return 1 if failures else 0
    except (SyncError, OSError, ValueError) as error:
        print(f"❌ {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
