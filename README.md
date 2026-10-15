# update-strava-weather

给 Strava 活动追加 gearaut 风格的天气标注（标题加天气 emoji，描述加温度/体感/湿度/风速块）。

只追加不覆盖：脚本会拉取活动当前内容，去掉旧的天气标注后再追加新的，保留 summitbag 等其它描述。

## 特性

- 一站式：OAuth 授权 / 令牌自动刷新 / 拉取活动 / 追加天气 / 更新
- 标题：AI 根据 weather_code、云量、时段判断主 emoji（🌞⛅☁️🌫️🌧️❄️⛈️🌙），体感 >30 加 🥵、<0 加 🥶
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
├── strava_config.example.sh        配置模板（提交）
├── strava_config.sh                真实密钥（gitignore）
├── .strava_tokens.json             令牌缓存（gitignore，自动生成）
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

`.claude/skills/strava-weather-update/SKILL.md` 提供端到端流程：本地脚本通过 Strava API 拉活动 → open-meteo 查天气 → 插值 min/max → AI 选 emoji → 本地脚本写入。在本仓库目录下对 Claude 说"更新我最新活动的天气"即可触发，无需连接 Strava MCP。

skill 优先使用活动的 `start_latlng`，缺少起点时再获取 `latlng` 坐标流；读写共用现有 OAuth 授权和令牌自动刷新。接口与权限见 [Strava 官方文档](https://developers.strava.com/docs/reference/)。

## 注意

- `strava_config.sh` 和 `.strava_tokens.json` 含敏感信息，已 gitignore，勿提交。
- Strava 访问令牌 6 小时过期，脚本自动用 refresh_token 刷新。
- 令牌刷新失效时会自动重新开浏览器授权。
- 两个脚本会将现有凭据文件权限收紧为 `600`，拒绝符号链接和硬链接；新 token 通过私有临时文件原子替换。
- 网络请求中的密钥、token 和授权码通过标准输入传递，脚本在读取凭据前关闭 Shell 跟踪。优先使用 `login` 完成授权。
- 更新所用临时文件放在权限为 `700` 的随机目录内，文件权限为 `600`，结束时自动清理。
- `DRY_RUN=1` 始终禁止活动写入，包括同时设置 `YES=1` 的情况，两个脚本均适用。
