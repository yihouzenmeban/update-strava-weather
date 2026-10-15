# update-strava-weather

给 Strava 活动追加 gearaut 风格的天气标注（标题加天气 emoji，描述加温度/体感/湿度/风速块）。

只追加不覆盖：脚本会拉取活动当前内容，去掉旧的天气标注后再追加新的，保留 summitbag 等其它描述。

## 特性

- 一站式：OAuth 授权 / 令牌自动刷新 / 拉取活动 / 追加天气 / 更新
- `sync` 一次完成检查、查天气和写入；缓存命中后只查一次活动列表，省去 AI 分步编排
- 标题：按 weather_code、当地时段和极值生成 emoji（🌞⛅☁️🌫️🌧️❄️⛈️🌙），体感 >30 加 🥵、<0 加 🥶，风速 >30 km/h 加 💨
- 描述：活动时段内各字段的 min - max（升序），保留 2 位小数
- 数据源：open-meteo（近期活动用 15 分钟 `minutely_15`，历史活动用 archive hourly）
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
├── .strava_weather_state.json      最近 128 条处理记录（gitignore，自动生成）
├── .claude/skills/strava-weather-update/SKILL.md   AI skill
├── .gitignore
└── README.md
```

## 首次配置

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

推荐直接同步最新一次活动：

```bash
./update_strava_weather.sh sync

# 只预览，不写活动和处理记录
./update_strava_weather.sh sync --dry-run

# 忽略处理记录和已有天气，强制重算；室内活动仍跳过
./update_strava_weather.sh sync --force

# 指定活动时不查列表；无 GPS 时可同时提供经纬度
./update_strava_weather.sh sync --activity-id 123 --latitude 39.9 --longitude 116.4
```

`sync` 默认直接补齐缺失天气。它只处理最新一次活动：先查列表，命中本地记录就结束；未命中时读取详情，已有完整天气块或属于室内活动则跳过并记录。需要补天气时使用起点坐标（缺少时获取轨迹流），自动插值、生成 emoji 和描述，再通过原更新流程读取最新描述并写入，保留其它备注和 summitbag。

成功更新、已有天气、室内跳过均记录；失败、空列表及预览不记录。计算期间如果其它工具已补上天气，写入前会再次检查并保留它。记录和并发锁权限为 `600`，同一仓库同时只能运行一个 `sync`。手动删除天气后请用 `--force` 重算；处理记录损坏时会报错，可在没有 sync 运行时移走 `.strava_weather_state.json` 后重新检查。

天气取值沿用活动开始时间加 `moving_time` 的区间，包含区间内采样点及首尾插值。天气请求统一用 UTC 时间戳，emoji 的夜间判断使用 `start_date_local` 的当地钟表时间，避免重复换算时区。近期活动用 forecast `minutely_15`，超过 5 天用 archive `hourly`，不会对缺失数据外推。

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
- `sync [--force] [--dry-run] [--activity-id ID] [--latitude LAT --longitude LNG]` — 自动补齐天气并记录处理结果
- `login` — 浏览器授权换令牌
- `auth` — 从标准输入读取授权码并换令牌；终端输入不回显，避免写进命令行参数和历史
- `list [数量]` — 获取最近的活动，默认 1 条，最多 200 条（`GET /athlete/activities`）
- `get` — 获取 `ACTIVITY_ID` 对应的活动详情（`GET /activities/{id}`）
- `streams` — 获取 `ACTIVITY_ID` 对应的坐标流（`GET /activities/{id}/streams`，`keys=latlng`）
- `show` — 等同 `DRY_RUN=1`，仅预览
- 不带参数 — 拉取活动、追加天气、确认后更新

环境变量：
- `ACTIVITY_ID`（`get` / `streams` / 更新时必填）活动 ID
- `WEATHER_EMOJI` / `WEATHER_EMOJI_FILE` 追加到标题的 emoji
- `WEATHER_DESC` / `WEATHER_DESC_FILE` 追加到描述的天气块
- `DRY_RUN=1` 仅预览
- `YES=1` 跳过确认

## 天气块模板

```
温度: {t_min}°C - {t_max}°C  体感温度: {at_min}°C - {at_max}°C
湿度: {h_min}% - {h_max}%  风速: {w_min} km/h - {w_max} km/h
Powered By www.gearaut.com
```

## AI Skill

`.claude/skills/strava-weather-update/SKILL.md` 默认只调用一次 `./update_strava_weather.sh sync` 并报告结果，活动检查、天气计算和写入均由脚本完成。在本仓库目录下对 Claude 说"更新我最新活动的天气"即可触发，无需连接 Strava MCP。

skill 优先使用活动的 `start_latlng`，缺少起点时再获取 `latlng` 坐标流；读写共用现有 OAuth 授权和令牌自动刷新。接口与权限见 [Strava 官方文档](https://developers.strava.com/docs/reference/)。

## 注意

- `strava_config.sh` 和 `.strava_tokens.json` 含敏感信息，已 gitignore，勿提交。
- Strava 访问令牌 6 小时过期，脚本自动用 refresh_token 刷新。
- 令牌刷新失效时会自动重新开浏览器授权。
- 两个脚本会将现有凭据文件权限收紧为 `600`，拒绝符号链接和硬链接；新 token 通过私有临时文件原子替换。
- 网络请求中的密钥、token 和授权码通过标准输入传递，脚本在读取凭据前关闭 Shell 跟踪。优先使用 `login` 完成授权。
- 更新所用临时文件放在权限为 `700` 的随机目录内，文件权限为 `600`，结束时自动清理。
- `DRY_RUN=1` 始终禁止活动写入，包括同时设置 `YES=1` 的情况，两个脚本均适用。
- 处理记录与运行日志也含个人活动信息，已 gitignore，勿提交。
