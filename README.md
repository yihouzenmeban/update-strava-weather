# updateStravaWeather

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
updateStravaWeather/
├── update_strava_weather.sh        主脚本
├── strava_config.example.sh        配置模板（提交）
├── strava_config.sh                真实密钥（gitignore）
├── .strava_tokens.json             令牌缓存（gitignore，自动生成）
├── .claude/skills/strava-weather-update.md   AI skill
├── .gitignore
└── README.md
```

## 首次配置

1. 到 https://www.strava.com/settings/api 注册应用，拿到 Client ID / Client Secret。
2. 把应用的 **Authorization Callback Domain** 设为 `localhost`。
3. 复制配置模板并填入密钥：
   ```bash
   cp strava_config.example.sh strava_config.sh
   # 编辑 strava_config.sh 填入 CLIENT_ID / CLIENT_SECRET
   ```
4. 授权（首次）：
   ```bash
   ./update_strava_weather.sh login
   ```
   会自动开浏览器授权，回调到本地服务自动换令牌并缓存。授权 scope 含 `activity:read_all,activity:write`。

## 用法

更新单个活动（交互确认）：
```bash
ACTIVITY_ID=123 \
WEATHER_EMOJI='☁️🥵' \
WEATHER_DESC_FILE=/tmp/desc.txt \
./update_strava_weather.sh
```

仅预览不写入：
```bash
ACTIVITY_ID=123 WEATHER_EMOJI='☁️🥵' WEATHER_DESC_FILE=/tmp/desc.txt ./update_strava_weather.sh show
```

跳过确认直接写入（自动化）：
```bash
ACTIVITY_ID=123 WEATHER_EMOJI='☁️🥵' WEATHER_DESC_FILE=/tmp/desc.txt YES=1 ./update_strava_weather.sh
```

子命令：
- `login` — 浏览器授权换令牌
- `auth <code>` — 手动用授权码换令牌
- `show` — 等同 `DRY_RUN=1`，仅预览
- 不带参数 — 拉取活动、追加天气、确认后更新

环境变量：
- `ACTIVITY_ID`（必填）要更新的活动 ID
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

`.claude/skills/strava-weather-update.md` 提供端到端流程：MCP 拉活动 → open-meteo 查天气 → 插值 min/max → AI 选 emoji → 预览确认 → 写入。在本仓库目录下对 Claude 说"更新我最新活动的天气"即可触发。

## 注意

- `strava_config.sh` 和 `.strava_tokens.json` 含敏感信息，已 gitignore，勿提交。
- Strava 访问令牌 6 小时过期，脚本自动用 refresh_token 刷新。
- 令牌刷新失效时会自动重新开浏览器授权。
