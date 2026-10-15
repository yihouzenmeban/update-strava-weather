---
name: strava-weather-update
description: 通过本地脚本直接调用 Strava API 拉取活动，按活动时间/地点查询天气，按 gearaut 风格生成标题和描述（含天气 emoji、体感超过 30 加 🥵、体感低于 0 加 🥶），并更新到 Strava。当用户说"更新 Strava 天气 / 给活动加天气 / 同步天气到 Strava"或类似时使用。
---

# Strava 活动天气标注

把最新一次 Strava 活动补上 gearaut 风格的天气标题和描述。流程：本地脚本通过 Strava API 拉活动 → open-meteo 拉天气 → 按规则生成标题/描述 → 本地脚本更新。

## 前置条件

- 本地脚本 `update_strava_weather.sh` 已配好 `strava_config.sh`（CLIENT_ID / CLIENT_SECRET），且 Strava 应用的 **Authorization Callback Domain = localhost**。
- 本地可用 Bash、curl 和 Python 3。在仓库根目录执行下列命令，无需 Strava MCP。
- 脚本首次运行会自动开浏览器授权（`activity:read_all,activity:write`）、缓存令牌到 `.strava_tokens.json`，之后自动刷新。读写均复用这套授权，不要读取或输出配置中的密钥、令牌。
- `list`、`get`、`streams` 成功时标准输出只有 API JSON；授权和刷新提示写到标准错误。命令失败时先处理错误，不继续计算或写入天气。
- 下列示例的 `123` 为虚构活动 ID，执行前必须替换为本次查询返回或用户指定的 ID。授权优先使用 `login`；手动 `auth` 从标准输入读取授权码，不将凭据放进命令行参数。

## 执行步骤

### 1. 拉最新活动

执行：

```bash
./update_strava_weather.sh list
```

脚本调用 `GET /athlete/activities?page=1&per_page=1`。取返回数组的第一条活动；空数组表示没有可用活动，告知用户并结束。记下 API 原始字段：

- `id`
- `name`（原始标题）
- `start_date_local`（本地开始时间；按当地钟表时间理解，不能把末尾的 `Z` 再换算一次时区）
- `moving_time`（秒，顶层字段）
- `start_latlng`（起点 `[lat, lng]`，可能为空）
- `trainer` / `sport_type`（跑步机或 Virtual* 活动跳过天气更新，与写入脚本保持一致）

如果用户指定了某个活动 ID，跳过列表请求，直接执行下列命令，通过 `GET /activities/{id}` 获取上述字段：

```bash
ACTIVITY_ID=123 ./update_strava_weather.sh get
```

### 2. 拿活动起点坐标

优先使用 `start_latlng` 中有效的 `[lat, lng]` 作为天气查询点。若为空或无效，再执行：

```bash
ACTIVITY_ID=123 ./update_strava_weather.sh streams
```

脚本调用 `GET /activities/{id}/streams?keys=latlng&key_by_type=true`，取返回的 `latlng.data` 中第一个有效的 `[lat, lng]`。API 使用 `latlng`，不是 MCP 的 `location`。

- 如果活动详情可正常读取，但没有坐标流（缺少 `latlng.data`、数据为空或 streams 返回 404），向用户询问活动发生的城市或地点，用 open-meteo 的 geocoding 拿坐标；若用户确认是常用地点（如望京），可用兜底坐标。
- 401、403、429 或网络错误按下方错误处理执行，不当作无 GPS 继续处理。

### 3. 查天气

用 Bash 调 open-meteo（无需 key），**优先用 15 分钟间隔的 `minutely_15`**，时区 `Asia/Shanghai`：

```bash
curl -s "https://api.open-meteo.com/v1/forecast?latitude=LAT&longitude=LNG&minutely_15=temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m,cloud_cover,weather_code,precipitation&timezone=Asia%2FShanghai&past_days=3&forecast_days=2"
```

> 近期活动（几天内）用上面的 forecast `minutely_15`。**历史活动（约 5 天前及更早）forecast 拉不到，改用 archive API（仅 hourly）**：
> ```bash
> curl -s "https://archive-api.open-meteo.com/v1/archive?latitude=LAT&longitude=LNG&start_date=DATE&end_date=DATE&hourly=temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m,cloud_cover,weather_code,precipitation&timezone=Asia%2FShanghai"
> ```
> archive 没有 `minutely_15`，用 hourly 即可。

### 4. 取活动时段内的最小值和最大值

- 活动开始时间 = `start_date_local`
- 活动结束时间 = 开始时间 + `moving_time` 秒
- 对 `temperature_2m`、`apparent_temperature`、`relative_humidity_2m`、`wind_speed_10m` 这四个字段，**在活动时段 [开始, 结束] 内取最小值和最大值**：
  - 收集所有时间戳落在 [开始, 结束] 区间内的 15 分钟（或 hourly）采样点；
  - 再补上开始、结束两个时刻的线性插值值，保证边界不被漏掉；
  - 从这个集合里取 `min` 和 `max`。
- 得到 8 个值：`t_min,t_max`（气温）、`at_min,at_max`（体感）、`h_min,h_max`（湿度）、`w_min,w_max`（风速）。
- 全部**保留 2 位小数**。
- 描述里一律按 **min - max（升序）** 排列，不按时间顺序。

### 5. 选天气主 emoji（AI 自己判断）

根据活动时段的天气（降水、weather_code、云量、能见度、风等）**自己判断**选一个最贴切的主 emoji，不要求死板对照。参考表（open-meteo 用 WMO weather_code）：

| weather_code | 含义 | emoji |
|---|---|---|
| 0 | 晴空 | 🌞 |
| 1 | 大部晴 | 🌞 |
| 2 | 多云 | ⛅ |
| 3 | 阴 | ☁️ |
| 45, 48 | 雾 / 霾 | 🌫️ |
| 51–57, 61–67, 80–82 | 毛毛雨 / 雨 | 🌧️ |
| 71–77, 85–86 | 雪 | ❄️ |
| 95–99 | 雷暴 | ⛈️ |

辅助参考（与 weather_code 冲突时以 weather_code 为主，AI 综合判断）：
- cloud_cover：<50% 🌞 / 50–85% ⛅ / >85% ☁️
- 夜间（本地 20:00–05:00）且无降水无雾 → 🌙
- 风速极大（>30 km/h）可在主 emoji 后再加 💨，但通常只取 1 个主 emoji
- 综合整个活动时段的天气选最合适的（如跑动中转雨用 🌧️），不只看开始时刻

> 通常只选 1 个主 emoji，根据实际天气选最贴切的。

### 6. 体感温度加 🥵 / 🥶

根据活动时段内体感温度（apparent_temperature）的极值（第 4 步已算出）：
- `at_max > 30` → 主 emoji 后追加 🥵
- `at_min < 0` → 主 emoji 后追加 🥶
- 两者都不触发就不加

例：体感 31.5°C → `☁️🥵`；体感 -2°C → `⛅🥶`；体感 15°C → `☁️`

### 7. 生成天气 emoji 和天气描述块

这一步只生成**要追加的内容**，不生成完整标题/描述（脚本会自己拉取活动当前内容并追加）。

**WEATHER_EMOJI**（追加到标题末尾的 emoji 串）：主 emoji（第 5 步 AI 判断）+ 若第 6 步触发则再拼 `🥵` 或 `🥶`。
- 例：`☁️🥵`、`🌞`、`🌧️🥵`、`🌫️`、`⛅🥶`

**WEATHER_DESC**（追加到描述末尾的天气块，严格按此模板，值用第 4 步的 min/max，保留 2 位小数）：

```
温度: {t_min}°C - {t_max}°C  体感温度: {at_min}°C - {at_max}°C
湿度: {h_min}% - {h_max}%  风速: {w_min} km/h - {w_max} km/h
Powered By www.gearaut.com
```

注意：所有字段都是 **min - max（升序）**，不是起点/终点顺序。

### 8. 直接写入 Strava（不用等用户确认）

把 WEATHER_EMOJI 和 WEATHER_DESC 写进临时文件（避免 shell 引号/换行问题），然后直接 `YES=1` 写入，**无需先跑 dry-run、无需询问用户确认**：

```bash
(
umask 077
weather_dir=$(mktemp -d "${TMPDIR:-/tmp}/strava-weather-input.XXXXXX")
trap 'rm -rf "$weather_dir"' EXIT
printf '%s' '☁️🥵' > "$weather_dir/emoji.txt"
printf '%s' '温度: 25.25°C - 27.39°C  体感温度: 29.59°C - 31.53°C
湿度: 82.83% - 70.38%  风速: 3.58 km/h - 2.81 km/h
Powered By www.gearaut.com' > "$weather_dir/description.txt"

ACTIVITY_ID=123 \
WEATHER_EMOJI_FILE="$weather_dir/emoji.txt" \
WEATHER_DESC_FILE="$weather_dir/description.txt" \
YES=1 ./update_strava_weather.sh
)
```

脚本流程：
1. 拉取活动当前的 name 和 description
2. 标题：去掉末尾已有的天气 emoji，追加 `WEATHER_EMOJI`
3. 描述：去掉旧的 `温度:...Powered By www.gearaut.com` 块，追加 `WEATHER_DESC`
4. PUT 更新（`YES=1` 跳过交互确认）

令牌处理：有缓存且有效 → 直接用；过期 → 自动刷新；无令牌或刷新失败 → 自动开浏览器授权（提示用户点 Authorize）。

成功标志：脚本输出 `✅ 更新成功` 并打印更新后的 name / description。脚本输出里会包含原/新标题和描述对比，把它连同最终结果一起贴给用户即可。

### 9. 收尾

- 更新成功后，把最终更新后的标题和描述贴给用户。
- 清理临时文件。

错误处理：
- 读命令遇到 HTTP 错误、网络错误或无效 JSON 会返回非零退出码；保留标准错误用于判断原因。
- 401 / 403：提醒用户运行 `./update_strava_weather.sh login` 重新授权，并确保授予 `activity:read_all,activity:write`。
- 活动详情报 404：检查活动 ID、令牌所属账号和活动访问权限；不要把它当作无 GPS。仅 streams 报 404 时，按第 2 步处理。
- 429：请求达到限额，停止本次流程，稍后重试。
- 如果报 "Application invalid"：Strava 应用 Callback Domain 没设成 `localhost`，提醒去 https://www.strava.com/settings/api 改。
- 如果报 "invalid encoding"：用 `*_FILE` 方式传值（脚本已内置），不要用 `-d` 内联 JSON。

## 批量更新

用户可一次给多个活动 ID。每个 ID 先用 `get` 获取活动详情，再走第 2–9 步：拉坐标、查天气、生成、预览。可把 5 个预览汇总成一张表一次性贴给用户确认，确认后用循环逐个 `YES=1` 更新。

## 规则速查

- 主 emoji：AI 根据 weather_code / 云量 / 时段综合判断（🌞⛅☁️🌫️🌧️❄️⛈️🌙 等），通常 1 个
- 取值：活动时段内各字段的 **min 和 max**（非起点/终点）
- 🥵 触发：`活动时段体感最大值 > 30`
- 🥶 触发：`活动时段体感最小值 < 0`
- 描述模板：`温度 / 体感` 一行，`湿度 / 风速` 一行，`Powered By www.gearaut.com` 一行
- 所有数值保留 2 位小数
- 值一律 **min - max（升序）** 排列
- 近期活动用 forecast `minutely_15`（15 分钟），历史活动用 archive `hourly`
