# update-strava-weather

给 Strava 活动追加 gearaut 风格的天气标注（标题加天气 emoji，描述加温度/体感/湿度/风速块）。

只追加不覆盖：脚本会拉取活动当前内容，去掉旧的天气标注后再追加新的，保留 summitbag 等其它描述。

## 特性

- 一站式：OAuth 授权 / 令牌自动刷新 / 拉取活动 / 追加天气 / 更新
- `sync` 分页发现新增活动并重试积压任务，按 ID 跳过已完成活动的详情和天气查询，省去 AI 分步编排
- 标题：按 weather_code、当地时段和极值生成 emoji（🌞⛅☁️🌫️🌧️❄️⛈️🌙），体感 >30 加 🥵、<0 加 🥶，风速 >30 km/h 加 💨
- 描述：活动时段内各字段的 min - max（升序），保留 2 位小数
- 数据源可选：Open-Meteo（默认，无需密钥）或和风天气（QWeather，需配置 API Host 和凭据）
- 覆盖前预览确认（`show` / `DRY_RUN=1`）
- 附带 Claude Code skill，可由 AI 端到端驱动

## 目录结构

```
update-strava-weather/
├── update_strava_weather.sh        主脚本
├── strava_common.sh               凭据权限和安全请求公共逻辑
├── strava_oauth.py                本地 OAuth 回调与 state 校验
├── strava_sync.py                 自动补齐天气、处理记录与并发锁
├── strava_config.example.sh        配置模板（提交）
├── strava_config.sh                真实密钥（gitignore）
├── .strava_tokens.json             令牌缓存（gitignore，自动生成）
├── .strava_weather_state.json      扫描进度、处理记录和待处理队列（gitignore，自动生成）
├── .claude/skills/strava-weather-update/SKILL.md   AI skill
├── .gitignore
└── README.md
```

## 首次配置

运行环境需要 Bash、Python 3.9+（使用标准库 `zoneinfo`）和 curl。

1. 到 https://www.strava.com/settings/api 注册应用，拿到 Client ID / Client Secret。
2. 把应用的 **Authorization Callback Domain** 设为 `localhost`。
3. 复制配置模板并填入密钥：
   ```bash
   cp strava_config.example.sh strava_config.sh
   chmod 600 strava_config.sh
   # 编辑 strava_config.sh 填入 CLIENT_ID / CLIENT_SECRET
   ```
4. 授权（首次）：
   ```bash
   ./update_strava_weather.sh login
   ```
   会自动开浏览器授权，回调到本地服务自动换令牌并缓存。授权 scope 含 `activity:read_all,activity:write`；回调校验随机 `state`，最多等待 5 分钟。

## 用法

以下 `123` 为虚构活动 ID，`/path/to/desc.txt` 为天气描述文件占位路径，使用时替换为实际值。描述文件应放在私有目录并设为 `600` 权限。

推荐批量同步新增活动及上次失败的任务：

```bash
./update_strava_weather.sh sync

# 只预览，不写活动和处理记录
./update_strava_weather.sh sync --dry-run

# 指定一条活动强制重算；室内活动仍跳过
./update_strava_weather.sh sync --activity-id 123 --force

# 按 UTC 日期补扫，包含起止当天；未结束的日期只查到本轮开始时刻
./update_strava_weather.sh sync --since 2026-10-01 --until 2026-10-15

# 补扫全部历史，已有完整天气和已完成活动仍跳过
./update_strava_weather.sh sync --all

# 指定活动时不查列表；无 GPS 时可同时提供经纬度
./update_strava_weather.sh sync --activity-id 123 --latitude 39.9 --longitude 116.4
```

`sync` 默认直接补齐缺失天气。首次扫描最近 7 天；之后从“上次完整扫描时间往前 7 天”查到本轮开始时刻，因此停用一段时间后也会覆盖整个停用期间。每页默认 50 条，可用 `--page-size 1–200` 调整；分页参数固定本轮时间上限，按 ID 去重，不会遇到已处理活动就提前停止。通常少量活动只需要一次列表请求，已完成的 ID 不再请求详情和天气。

发现的待处理 ID 每页先落盘，完整扫描后才推进扫描进度，再逐条处理队列。成功更新、已有完整天气和室内活动均立即记录完成；普通单条失败保留队列并继续其它活动，即使失败活动已不在下次查询范围内也会重试。遇到授权失败或 429 时停止本轮，保留尚未处理的 ID。部分失败时命令返回非零退出码，并报告完成、失败和剩余数量。

分页失败或首次扫描中断会保留原查询起点，不把未扫描范围当作完成；再次运行默认 `sync` 会覆盖原范围。空列表仍更新完整扫描时间。`--dry-run` 或 `DRY_RUN=1` 不写活动、队列、处理结果或扫描进度。

活动时间早于重叠范围的补传活动可能无法被默认扫描发现，请用 `--since` / `--until` 或 `--all` 补扫。显式日期范围和单个 ID 只处理所选活动，不处理范围外的积压，也不改变默认扫描进度；历史补扫分页失败后应重跑同一补扫命令。`--force` 重算本次查询范围内的活动，其强制重算标记会随失败任务保留到下次重试。

需要补天气时使用起点坐标（缺少时获取轨迹流），自动插值、生成 emoji 和描述，再通过原更新流程读取最新描述并写入，保留其它备注和 summitbag；计算期间如果其它工具已补上天气，写入前会再次检查并保留它。经纬度覆盖参数只允许配合 `--activity-id`，避免把同一地点用于不同活动。

旧版处理记录自动迁移到新格式，保留已有完成结果；新版不再只保留最后 128 条，以免大批历史补扫重复工作。记录和并发锁权限为 `600`，同一仓库同时只能运行一个 `sync`。手动删除天气后请指定 ID 加 `--force` 重算。记录损坏时会报错，不会自动清空；在没有 sync 运行时可先私有备份再移走记录，随后重新补扫，但原队列和扫描进度不会自动恢复。

天气取值沿用活动开始时间加 `moving_time` 的区间，包含区间内采样点及首尾插值。emoji 的夜间判断使用 `start_date_local` 的当地钟表时间。Open-Meteo 请求使用 UTC 时间戳，近期活动用 forecast `minutely_15`，超过 5 天用 archive `hourly`，不会对缺失数据外推。

### 选择和风天气

默认使用 Open-Meteo。可在 `strava_config.sh` 中设置 `WEATHER_PROVIDER="qweather"`，或临时选择：

```bash
./update_strava_weather.sh sync --weather-provider qweather --dry-run
```

选择和风天气前，从[和风控制台](https://console.qweather.com/)获取个人 API Host 和 API Key，通过环境变量提供：

```bash
export WEATHER_PROVIDER=qweather
export QWEATHER_API_HOST=你的个人域名.qweatherapi.com
# 交互输入 API Key，避免将凭据写进命令历史；此命令适用于 Bash / Zsh
read -rs QWEATHER_API_KEY
export QWEATHER_API_KEY
./update_strava_weather.sh sync --dry-run
./update_strava_weather.sh sync
```

`QWEATHER_API_HOST` 只填控制台分配的域名，不含 `https://` 或路径。也可用 `QWEATHER_JWT` 环境变量提供自行生成且尚未过期的 JWT；设置后优先使用 JWT，不同时发送 API Key。脚本不会生成或续期 JWT。认证只放在请求 Header 中，不放进 URL、日志或进程参数。详见[官方认证文档](https://dev.qweather.com/docs/configuration/authentication/)。

和风模式先通过坐标反查城市 ID 和时区，再读取城市的逐小时历史数据；返回时间按城市时区转为时间戳，跨日活动会查询所需各天。其[天气时光机接口](https://dev.qweather.com/docs/api/time-machine/time-machine-weather/)仅支持最近 10 天，**不包含今天**。当天活动会保留在待处理队列，等待后续同步重试；如果末尾插值需要今天的整点，也需要等待。超过 10 天的活动可用 `--weather-provider open-meteo` 补扫，不会自动混用数据源。

历史接口不提供体感温度，脚本使用温度、湿度、风速，按 [Steadman AT 的无辐射近似公式](https://www.bom.gov.au/info/thermal_stress/#atapproximation)在本地计算：

```text
e  = RH / 100 × 6.105 × exp(17.27 × T / (237.7 + T))
AT = T + 0.33 × e − 0.70 × (windSpeed / 3.6) − 4.00
```

`T` 为摄氏气温，`RH` 为相对湿度（0–100），`windSpeed` 为接口返回的 km/h 风速，公式使用 10 米高度风速口径。活动首尾先对齐并插值温湿风输入，再与时段内逐小时采样一起逐点计算 AT，取这些计算点的最小值和最大值；不把不同时间的温湿风极值拼在一起，也不直接插值 AT。描述中显示为“体感温度”，来源仍注明 `Powered By QWeather (www.qweather.com)`；体感 >30°C 加 🥵、<0°C 加 🥶。

该算法不考虑太阳直晒，结果为本地计算值，与和风官方体感温度的算法口径不一定相同。缺失、异常或不连续的采样会报错并保留队列，不会外推。已完成活动仍按原记录跳过，若需为旧的和风天气块补上体感温度，使用下方的 `--force` 命令重算。

命令行 `--weather-provider` 优先于 `WEATHER_PROVIDER`。切换数据源后，已完成活动仍保留原天气；如需替换，请指定活动并强制重算：

```bash
./update_strava_weather.sh sync --weather-provider qweather --activity-id 123 --force --dry-run
./update_strava_weather.sh sync --weather-provider qweather --activity-id 123 --force
```

需要定时运行时，可自行将下例加入 crontab（替换仓库路径；此仓库不会自动安装定时任务）：

```cron
*/5 * * * * umask 077; cd /path/to/update-strava-weather && /bin/bash ./update_strava_weather.sh sync >> .strava_weather_sync.log 2>&1
```

请先手动运行 `login` 和 `sync --dry-run` 验证环境。定时任务的 PATH 中需有 `python3` 和 `curl`；`sync` 在授权失效时退出并提示 `login`，不会无人值守地等待浏览器授权。日志包含活动标题和描述，已加入 gitignore。

直接读取 Strava API（复用本地授权，无需 MCP）：

```bash
# 最新一次活动，返回 JSON 数组；也可 list 5 获取最近 5 次（数量范围 1–200）
./update_strava_weather.sh list

# 活动详情，返回 JSON 对象
ACTIVITY_ID=123 ./update_strava_weather.sh get

# 活动坐标流，坐标位于 latlng.data
ACTIVITY_ID=123 ./update_strava_weather.sh streams
```

只读命令的标准输出仅包含 API JSON，可重定向到文件或交给 Python 解析；授权/刷新提示和错误写入标准错误。HTTP 错误、网络错误、无效 JSON 均以非零退出码结束，列表为空时返回 `[]`。

更新单个活动（交互确认）：
```bash
ACTIVITY_ID=123 \
WEATHER_EMOJI='☁️🥵' \
WEATHER_DESC_FILE=/path/to/desc.txt \
./update_strava_weather.sh
```

仅预览不写入：
```bash
ACTIVITY_ID=123 WEATHER_EMOJI='☁️🥵' WEATHER_DESC_FILE=/path/to/desc.txt ./update_strava_weather.sh show
```

跳过确认直接写入（自动化）：
```bash
ACTIVITY_ID=123 WEATHER_EMOJI='☁️🥵' WEATHER_DESC_FILE=/path/to/desc.txt YES=1 ./update_strava_weather.sh
```

子命令：
- `sync [--activity-id ID | --since DATE [--until DATE] | --all] [--page-size N] [--weather-provider open-meteo|qweather] [--force] [--dry-run]` — 批量增量同步或历史补扫
- `login` — 浏览器授权换令牌
- `auth` — 从标准输入读取授权码并换令牌；终端输入不回显，避免写进命令行参数和历史
- `list [数量]` — 获取最近的活动，默认 1 条，最多 200 条（`GET /athlete/activities`）
- `get` — 获取 `ACTIVITY_ID` 对应的活动详情（`GET /activities/{id}`）
- `streams` — 获取 `ACTIVITY_ID` 对应的坐标流（`GET /activities/{id}/streams`，`keys=latlng`）
- `show` — 等同 `DRY_RUN=1`，仅预览
- 不带参数 — 拉取活动、追加天气、确认后更新

环境变量：
- `ACTIVITY_ID`（`get` / `streams` / 更新时必填）活动 ID
- `WEATHER_PROVIDER` — `sync` 天气数据源，默认 `open-meteo`，可选 `qweather`
- `QWEATHER_API_HOST` / `QWEATHER_API_KEY` / `QWEATHER_JWT` — 和风天气个人域名与认证凭据
- `STRAVA_PAGE` / `STRAVA_AFTER` / `STRAVA_BEFORE` — `list` 的页码、开始和结束 Unix 时间戳，后两者为 API 原始排除边界
- `WEATHER_EMOJI` / `WEATHER_EMOJI_FILE` 追加到标题的 emoji
- `WEATHER_DESC` / `WEATHER_DESC_FILE` 追加到描述的天气块
- `DRY_RUN=1` 仅预览
- `YES=1` 跳过确认

## 天气块模板

Open-Meteo：

```
温度: {t_min}°C - {t_max}°C  体感温度: {at_min}°C - {at_max}°C
湿度: {h_min}% - {h_max}%  风速: {w_min} km/h - {w_max} km/h
Powered By www.gearaut.com
```

和风天气：

```
温度: {t_min}°C - {t_max}°C  体感温度: {at_min}°C - {at_max}°C
湿度: {h_min}% - {h_max}%  风速: {w_min} km/h - {w_max} km/h
Powered By QWeather (www.qweather.com)
```

## AI Skill

`.claude/skills/strava-weather-update/SKILL.md` 默认只调用一次 `./update_strava_weather.sh sync` 并报告结果，活动检查、天气计算和写入均由脚本完成。在本仓库目录下对 Claude 说"更新我最新活动的天气"即可触发，无需连接 Strava MCP。

skill 优先使用活动的 `start_latlng`，缺少起点时再获取 `latlng` 坐标流；读写共用现有 OAuth 授权和令牌自动刷新。接口与权限见 [Strava 官方文档](https://developers.strava.com/docs/reference/)。

## 注意

- `strava_config.sh` 和 `.strava_tokens.json` 含敏感信息，已 gitignore，勿提交。
- Strava 访问令牌 6 小时过期，脚本自动用 refresh_token 刷新。
- 令牌刷新失效时会自动重新开浏览器授权。
- 两个脚本会将现有凭据文件权限收紧为 `600`，拒绝符号链接和硬链接；新 token 通过私有临时文件原子替换。
- Strava 网络请求中的密钥、token 和授权码通过标准输入传递；和风天气凭据由 Python 从环境变量读取并放入请求 Header。脚本在读取凭据前关闭 Shell 跟踪。优先使用 `login` 完成授权。
- 更新所用临时文件放在权限为 `700` 的随机目录内，文件权限为 `600`，结束时自动清理。
- `DRY_RUN=1` 始终禁止活动写入，包括同时设置 `YES=1` 的情况，两个脚本均适用。
- 处理记录与运行日志也含个人活动信息，已 gitignore，勿提交。
