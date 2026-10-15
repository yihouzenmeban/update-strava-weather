#!/usr/bin/env python3
"""Synchronize weather once per activity, reusing the secure Strava shell commands."""

import argparse
from bisect import bisect_left, bisect_right
import datetime as dt
import fcntl
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


ROOT = Path(__file__).resolve().parent
FIELDS = ("temperature_2m", "apparent_temperature", "relative_humidity_2m", "wind_speed_10m")
VARIABLES = ",".join(FIELDS + ("weather_code",))
NUMBER = r"-?\d+(?:\.\d+)?"
WEATHER_BLOCK = re.compile(
    rf"^温度: {NUMBER}°C - {NUMBER}°C  体感温度: {NUMBER}°C - {NUMBER}°C\r?\n"
    rf"湿度: {NUMBER}% - {NUMBER}%  风速: {NUMBER} km/h - {NUMBER} km/h\r?\n"
    r"Powered By www\.gearaut\.com(?:\r?$)", re.M,
)


class SyncError(Exception):
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
        return {}
    with os.fdopen(fd, encoding="utf-8") as source:
        data = json.load(source)
    if not isinstance(data, dict):
        raise SyncError("处理记录格式错误，请检查 .strava_weather_state.json")
    if data.get("version") != 1:
        raise SyncError("处理记录版本无效，请检查 .strava_weather_state.json")
    activities = data.get("activities")
    if not isinstance(activities, dict) or any(
            not re.fullmatch(r"[1-9][0-9]*", key) or status not in ("updated", "existing", "indoor")
            for key, status in activities.items()):
        raise SyncError("处理记录格式错误，请检查 .strava_weather_state.json")
    return activities


def save_state(path, activities):
    fd, temporary = tempfile.mkstemp(prefix=".strava_weather_state.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump({"version": 1, "activities": dict(list(activities.items())[-128:])}, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def invoke(command, activity_id=None, extra=None):
    environment = dict(os.environ, STRAVA_NONINTERACTIVE="1")
    if activity_id is not None:
        environment["ACTIVITY_ID"] = str(activity_id)
    if extra:
        environment.update(extra)
    return subprocess.run(["bash", str(ROOT / "update_strava_weather.sh"), command],
                          env=environment, text=True, capture_output=True)


def read_strava(command, activity_id=None, allow_missing=False):
    result = invoke(command, activity_id)
    if result.returncode:
        if allow_missing and "HTTP 404" in result.stderr:
            return {}
        raise SyncError(result.stderr.strip() or f"Strava {command} 读取失败")
    return json.loads(result.stdout)


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def interval_extrema(times, values, start, end):
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
    samples = list(samples) + [interpolate(start), interpolate(end)]
    return min(samples), max(samples)


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


def coordinates(value):
    if isinstance(value, (list, tuple)) and len(value) == 2:
        latitude, longitude = value
        if numeric(latitude) and numeric(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180:
            return latitude, longitude
    return None


def weather_annotation(activity, location):
    try:
        start_date = dt.datetime.fromisoformat(activity["start_date"].replace("Z", "+00:00"))
        local_start = dt.datetime.fromisoformat(activity["start_date_local"].replace("Z", "+00:00")).replace(tzinfo=None)
        duration = activity["moving_time"]
        if start_date.tzinfo is None or not numeric(duration) or duration < 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise SyncError("活动缺少有效的 start_date、start_date_local 或 moving_time") from None
    start, end = start_date.timestamp(), start_date.timestamp() + duration
    url, key = weather_url(*location, start, end)
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = json.load(response)
    except (urllib.error.URLError, OSError, ValueError):
        raise SyncError("Open-Meteo 请求失败，未记录成功，下次可重试") from None
    series = payload.get(key, {}) if isinstance(payload, dict) else {}
    if not isinstance(series, dict):
        raise SyncError("Open-Meteo 未返回有效天气序列，未记录成功")
    times = series.get("time", [])
    ranges = {field: interval_extrema(times, series.get(field, []), start, end) for field in FIELDS}
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
    if ranges["apparent_temperature"][1] > 30:
        emoji += "🥵"
    if ranges["apparent_temperature"][0] < 0:
        emoji += "🥶"
    temperature, apparent, humidity, wind = (ranges[field] for field in FIELDS)
    description = (f"温度: {temperature[0]:.2f}°C - {temperature[1]:.2f}°C  体感温度: {apparent[0]:.2f}°C - {apparent[1]:.2f}°C\n"
                   f"湿度: {humidity[0]:.2f}% - {humidity[1]:.2f}%  风速: {wind[0]:.2f} km/h - {wind[1]:.2f} km/h\n"
                   "Powered By www.gearaut.com")
    return emoji, description


def synchronize(args, activities):
    if args.activity_id is not None:
        activity_id = args.activity_id
    else:
        latest = read_strava("list")
        if not isinstance(latest, list):
            raise SyncError("Strava 活动列表格式无效")
        if not latest:
            print("⏭️ 暂无可处理活动")
            return None
        if not isinstance(latest[0], dict):
            raise SyncError("Strava 活动列表格式无效")
        activity_id = str(latest[0].get("id", ""))
    if not re.fullmatch(r"[1-9][0-9]*", activity_id):
        raise SyncError("Strava 活动 ID 无效")
    if not args.force and activity_id in activities:
        print(f"⏭️ 活动 {activity_id} 已处理，跳过详情和天气查询")
        return None
    activity = read_strava("get", activity_id)
    if not isinstance(activity, dict) or str(activity.get("id")) != activity_id:
        raise SyncError("Strava 返回的活动与请求 ID 不一致")
    if activity.get("trainer") or str(activity.get("sport_type", "")).startswith("Virtual"):
        print(f"⏭️ 活动 {activity_id} 为室内活动，跳过")
        return activity_id, "indoor"
    if not args.force and has_weather(activity.get("description")):
        print(f"⏭️ 活动 {activity_id} 已有完整天气块，跳过")
        return activity_id, "existing"
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
    emoji, description = weather_annotation(activity, location)
    extra = {"WEATHER_EMOJI": emoji, "WEATHER_DESC": description, "WEATHER_EMOJI_FILE": "", "WEATHER_DESC_FILE": "",
             "YES": "1", "DRY_RUN": "1" if args.dry_run else "0",
             "STRAVA_SYNC_CHECK_WEATHER": "0" if args.force else "1"}
    result = invoke("show" if args.dry_run else "run", activity_id, extra)
    if result.returncode not in (0, 2, 3):
        raise SyncError(result.stderr.strip() or "Strava 写入失败，未记录成功")
    print(result.stdout.strip())
    return activity_id, {0: "updated", 2: "indoor", 3: "existing"}[result.returncode]


def main():
    parser = argparse.ArgumentParser(description="为最新活动补充天气，已处理活动自动跳过")
    parser.add_argument("--activity-id", help="指定活动 ID，跳过列表查询")
    parser.add_argument("--force", action="store_true", help="忽略缓存和已有天气，重新计算；室内活动仍跳过")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不写活动和处理记录")
    parser.add_argument("--latitude", type=float, help="无 GPS 时指定纬度")
    parser.add_argument("--longitude", type=float, help="无 GPS 时指定经度")
    args = parser.parse_args()
    args.dry_run = args.dry_run or os.environ.get("DRY_RUN") == "1"
    if (args.latitude is None) != (args.longitude is None) or (
            args.latitude is not None and coordinates([args.latitude, args.longitude]) is None):
        parser.error("--latitude 和 --longitude 必须同时提供，且在有效经纬度范围内")
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
            activities = load_state(state_path)
            result = synchronize(args, activities)
            if result is not None and not args.dry_run:
                activity_id, status = result
                activities.pop(activity_id, None)
                activities[activity_id] = status
                save_state(state_path, activities)
    except (SyncError, OSError, ValueError) as error:
        print(f"❌ {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
