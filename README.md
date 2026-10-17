# update-strava-weather

给 Strava 活动追加 gearaut 风格的天气标注（标题加天气 emoji，描述加温度/体感/湿度/风速块）。

只追加不覆盖：脚本会拉取活动当前内容，去掉旧的天气标注后再追加新的，保留 summitbag 等其它描述。

## 特性

- 一站式：OAuth 授权 / 令牌自动刷新 / 拉取活动 / 追加天气 / 更新
- `sync` 分页发现新增活动并重试积压任务，已完成活动不重查天气；默认检查山峰排序，关闭后按 ID 跳过详情
- 标题：按 weather_code、当地时段和极值生成 emoji（🌞⛅☁️🌫️🌧️❄️⛈️🌙），体感 >30 加 🥵、<0 加 🥶，风速 >30 km/h 加 💨
- 描述：活动时段内各字段的 min - max（升序），保留 2 位小数
- 独立文本模板：自定义天气描述的字段、排版、精度和署名，默认输出保持原样
- 山峰排序：默认将 summitbag / 山峰 emoji 行移到天气前，可配置关闭
- 数据源：Open-Meteo（无需密钥；近期活动用 15 分钟 `minutely_15`，历史活动用 archive hourly）
- 覆盖前预览确认（`show` / `DRY_RUN=1`）
- 附带共享 Agent 技能，Claude 入口通过软链复用

## 目录结构

```
update-strava-weather/
├── update_strava_weather.sh        主脚本
├── strava_common.sh               凭据权限和安全请求公共逻辑
├── strava_oauth.py                本地 OAuth 回调与 state 校验
├── strava_sync.py                 自动补齐天气、处理记录与并发锁
├── weather_template.py            天气模板渲染、识别与替换
├── templates/weather.default.txt  默认天气描述模板（提交）
├── weather.custom.txt             可选个人模板（gitignore）
├── strava_config.example.sh        配置模板（提交）
├── strava_config.sh                真实密钥（gitignore）
├── .strava_tokens.json             令牌缓存（gitignore，自动生成）
├── .strava_weather_state.json      扫描进度、处理记录和待处理队列（gitignore，自动生成）
├── AGENTS.md                      共享中文贡献指南
├── CLAUDE.md -> AGENTS.md          Claude 规则入口
├── .agents/skills/strava-weather-update/SKILL.md   共享技能源文件
├── .claude/skills -> ../.agents/skills             Claude 技能兼容入口
├── .gitignore
└── README.md
```

## 首次配置

运行环境需要 Bash、Python 3.9+ 和 curl。

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

# 补扫全部历史，已有天气标注和已完成活动仍跳过
./update_strava_weather.sh sync --all

# 指定活动时不查列表；无 GPS 时可同时提供经纬度
./update_strava_weather.sh sync --activity-id 123 --latitude 39.9 --longitude 116.4
```

`sync` 默认直接补齐缺失天气。首次扫描最近 7 天；之后从“上次完整扫描时间往前 7 天”查到本轮开始时刻，因此停用一段时间后也会覆盖整个停用期间。每页默认 50 条，可用 `--page-size 1–200` 调整；分页参数固定本轮时间上限，按 ID 去重，不会遇到已处理活动就提前停止。通常少量活动只需要一次列表请求。默认开启山峰排序，扫描范围内已完成的户外活动会再读取一次详情以检查后来追加的山峰数据，但不重查天气；关闭山峰排序后，已完成的 ID 跳过详情和天气查询。已记录的室内活动仍直接跳过。

发现的待处理 ID 每页先落盘，完整扫描后才推进扫描进度，再逐条处理队列。成功更新、已有天气标注和室内活动均立即记录完成；普通单条失败保留队列并继续其它活动，即使失败活动已不在下次查询范围内也会重试。遇到授权失败或 429 时停止本轮，保留尚未处理的 ID。部分失败时命令返回非零退出码，并报告完成、失败和剩余数量。

分页失败或首次扫描中断会保留原查询起点，不把未扫描范围当作完成；再次运行默认 `sync` 会覆盖原范围。空列表仍更新完整扫描时间。`--dry-run` 或 `DRY_RUN=1` 不写活动、队列、处理结果或扫描进度。

活动时间早于重叠范围的补传活动可能无法被默认扫描发现，请用 `--since` / `--until` 或 `--all` 补扫。显式日期范围和单个 ID 只处理所选活动，不处理范围外的积压，也不改变默认扫描进度；历史补扫分页失败后应重跑同一补扫命令。`--force` 重算本次查询范围内的活动，其强制重算标记会随失败任务保留到下次重试。

每条活动详情只读取一次，后续判断、天气计算和描述合并共用这份数据。需要补天气时使用起点坐标（缺少时获取轨迹流），自动插值、生成 emoji 和描述，保留其中的手写备注及 summitbag。更新只提交实际变化的字段：仅排序时只写 `description`，标题变化时才提交 `name`，全部无变化时不 PUT。正常单条更新为 1 次详情 GET + 1 次 PUT；本地同步锁避免本脚本并行写入，外部工具在读取后修改同一字段仍可能被覆盖。经纬度覆盖参数只允许配合 `--activity-id`，避免把同一地点用于不同活动。

旧版处理记录自动迁移到新格式，保留已有完成结果；新版不再只保留最后 128 条，以免大批历史补扫重复工作。记录和并发锁权限为 `600`，同一仓库同时只能运行一个 `sync`。手动删除天气后请指定 ID 加 `--force` 重算。记录损坏时会报错，不会自动清空；在没有 sync 运行时可先私有备份再移走记录，随后重新补扫，但原队列和扫描进度不会自动恢复。

天气取值沿用活动开始时间加 `moving_time` 的区间，包含区间内采样点及首尾插值。emoji 的夜间判断使用 `start_date_local` 的当地钟表时间。Open-Meteo 请求使用 UTC 时间戳，近期活动用 forecast `minutely_15`，超过 5 天用 archive `hourly`，不会对缺失数据外推。

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
- `sync [--activity-id ID | --since DATE [--until DATE] | --all] [--page-size N] [--force] [--dry-run]` — 批量增量同步或历史补扫
- `login` — 浏览器授权换令牌
- `auth` — 从标准输入读取授权码并换令牌；终端输入不回显，避免写进命令行参数和历史
- `list [数量]` — 获取最近的活动，默认 1 条，最多 200 条（`GET /athlete/activities`）
- `get` — 获取 `ACTIVITY_ID` 对应的活动详情（`GET /activities/{id}`）
- `streams` — 获取 `ACTIVITY_ID` 对应的坐标流（`GET /activities/{id}/streams`，`keys=latlng`）
- `show` — 等同 `DRY_RUN=1`，仅预览
- 不带参数 — 拉取活动、追加天气、确认后更新

环境变量：
- `ACTIVITY_ID`（`get` / `streams` / 更新时必填）活动 ID
- `STRAVA_PAGE` / `STRAVA_AFTER` / `STRAVA_BEFORE` — `list` 的页码、开始和结束 Unix 时间戳，后两者为 API 原始排除边界
- `WEATHER_EMOJI` / `WEATHER_EMOJI_FILE` 追加到标题的 emoji
- `WEATHER_DESC` / `WEATHER_DESC_FILE` 追加到描述的天气块
- `WEATHER_TEMPLATE_FILE` 天气描述模板路径，用于 `sync` 生成及更新时识别旧天气块
- `REORDER_SUMMITBAG` 默认 `1`，将山峰数据排到天气前；设为 `0` 关闭
- `DRY_RUN=1` 仅预览
- `YES=1` 跳过确认

## 运行模式（二选一，可并存）

**模式一：定时任务（独立运行，开箱即用）**
适合只想"每隔一段时间扫一遍新活动"的用户，不需要公网回调。
用 cron / systemd timer 定时跑 `./update_strava_weather.sh sync`
（成功静默、失败告警）；历史补扫用 `./backfill_all.sh`。
Strava 应用默认限流 100 次/15 分钟、1000 次/天，轮询完全够用。

**模式二：webhook 实时推送（需要公网回调）**
Strava 有新活动时官方把事件推送到你的回调地址，
`webhook/receiver.py` 收到后默认等 60 秒再触发单活动同步，可通过 `sync_delay_seconds` 配置或 `WEBHOOK_SYNC_DELAY_SECONDS` 环境变量调整。
起点坐标完整时，每个需要更新的新活动使用 2 次 Strava API 调用。详见 `webhook/README.md`。
两种模式可并存（webhook 为主、定时为兜底），也可只选其一。

## 天气块模板

默认模板位于 `templates/weather.default.txt`，保持原有字段、两位小数、空格、换行和署名：

```text
温度: {t_min:.2f}°C - {t_max:.2f}°C  体感温度: {at_min:.2f}°C - {at_max:.2f}°C
湿度: {h_min:.2f}% - {h_max:.2f}%  风速: {w_min:.2f} km/h - {w_max:.2f} km/h
Not Powered By www.gearaut.com 🍚
```

自定义时先复制，再编辑 `weather.custom.txt`（已 gitignore）：

```bash
cp templates/weather.default.txt weather.custom.txt
```

例如改成：

```text
{emoji} 本次运动天气
🌡️ {t_min:.1f}～{t_max:.1f}°C，体感 {at_min:.1f}～{at_max:.1f}°C
💧 {h_min:.0f}～{h_max:.0f}% ｜ 💨 {w_min:.1f}～{w_max:.1f} km/h
Not Powered By www.gearaut.com 🍚
```

| 字段 | 含义与单位 |
| --- | --- |
| `{emoji}` | 现有规则计算的天气 emoji，标题规则保持原样 |
| `{t_min}` / `{t_max}` | 最低／最高温度，°C |
| `{at_min}` / `{at_max}` | 最低／最高体感温度，°C |
| `{h_min}` / `{h_max}` | 最低／最高湿度，% |
| `{w_min}` / `{w_max}` | 最低／最高风速，km/h |

数值默认保留两位小数，可使用 `.0f` 至 `.6f` 指定精度；单位和文案由模板控制，删除某行即可隐藏相应字段。模板须为 UTF-8，包含固定文案以识别天气块。字面量花括号写成 `{{` 和 `}}`。不支持表达式、属性访问或条件语法；模板不存在、为空或字段错误时，`sync` 在请求 API 和写入处理记录之前报错。

在 `strava_config.sh` 中配置后，手动运行、cron 和 webhook 都会使用该模板：

```bash
WEATHER_TEMPLATE_FILE="weather.custom.txt"
```

也可以临时选用并预览（`123` 替换为实际活动 ID）：

```bash
./update_strava_weather.sh sync --activity-id 123 --force --dry-run --template weather.custom.txt
```

优先级为 `--template` > 环境变量 `WEATHER_TEMPLATE_FILE` > 配置文件 > 默认模板。相对路径统一以仓库根目录为准。`WEATHER_DESC_FILE` 仍表示已经生成好的描述文本，不会解析其中的占位符。

**防重复与更新规则：** 描述包含 `www.gearaut.com`，或完整匹配当前模板的固定文案、字段位置与换行，即视为已有天气。数值和天气 emoji 按类型匹配，不要求等于本次计算结果；同时兼容历史 gearaut / QWeather 天气块。只有标题 emoji 时仍会补齐描述。每次同步基于本次读取的数据判断，失败重试时重新读取并计算。

默认同步不重算已有天气及已处理 ID 的天气，但仍按下述规则检查山峰排序；更换模板后，可指定活动加 `--force` 更新天气。强制更新仅移除能定位的旧格式或当前模板天气块，保留前后备注及 summitbag。若含域名但无法定位旧天气块，会报错而不追加。`reorder_summitbag.sh` 使用同一模板配置识别天气块。

建议个人模板保留 `www.gearaut.com` 署名，以便改版后仍能防重复。无署名的自定义模板只能按当前结构识别：修改固定文案或换行前，请保留旧模板，必要时手动清理旧天气块后再强制更新。

## 山峰数据排序

默认开启，顺序为「天气前的手写备注 → 山峰数据 → 天气」。识别独立的 summitbag 链接行，以及带山峰 emoji / summitbag 标记和括号内海拔的山峰数据行（如 `⛰️ 馒头峪 (767 m)`）。山峰行内部内容及先后顺序保持原样；即使山峰原本位于手写备注上方，也会移到这些备注之后。普通的 `⛰️ 今天的手写游记` 不视为山峰数据；天气块里的同类符号不会被拆走，天气后的其它备注保留原位置。兼容旧 gearaut / QWeather 格式及当前自定义模板。

例如已有描述为：

```text
今天手写的路线记录

温度: 13.58°C - 21.48°C 体感温度: 13.60°C - 22.50°C
湿度: 89% - 69% 风速: 2.98 km/h - 3.95 km/h
Powered By www.gearaut.com

⛰️ 馒头峪 (767 m) • ⛰️ 萝芭地北尖 (1,146 m) | 🌐 summitbag.com
```

同步后变为：

```text
今天手写的路线记录

⛰️ 馒头峪 (767 m) • ⛰️ 萝芭地北尖 (1,146 m) | 🌐 summitbag.com
温度: 13.58°C - 21.48°C 体感温度: 13.60°C - 22.50°C
湿度: 89% - 69% 风速: 2.98 km/h - 3.95 km/h
Powered By www.gearaut.com
```

仅排序时保留原天气数值、格式和活动标题，不需要 GPS 或天气查询，只更新 `description`。顺序正确、没有山峰、或无法定位天气块时不写入。失败保留到同步队列重试，`--dry-run` 仅预览。

```bash
# 单条活动检查并预览；已有处理记录也会检查，不需要 --force
./update_strava_weather.sh sync --activity-id 123 --dry-run

# 扫描全部历史并检查排序；已有天气不会重算
./update_strava_weather.sh sync --all --dry-run

# 本次关闭山峰排序
./update_strava_weather.sh sync --no-reorder-summitbag
```

长期关闭可在 `strava_config.sh` 中设置 `REORDER_SUMMITBAG=0`，也可通过同名环境变量临时覆盖。优先级为命令行 `--reorder-summitbag` / `--no-reorder-summitbag` > 环境变量 > 配置文件，默认 `1`。该配置同时适用于手动更新和独立的 `reorder_summitbag.sh`；独立脚本继续只处理越野跑活动。

## AI Skill

共享技能位于 `.agents/skills/strava-weather-update/SKILL.md`，`.claude/skills` 通过相对软链复用该目录。技能默认只调用一次 `./update_strava_weather.sh sync` 并报告结果，活动检查、天气计算和写入均由脚本完成。在本仓库目录下对 Claude 说"更新我最新活动的天气"即可触发，无需连接 Strava MCP。

只维护 `AGENTS.md` 和 `.agents/` 中的真实文件，`CLAUDE.md` 与 `.claude/skills` 保留为软链，避免多份内容不一致。脚本优先使用活动的 `start_latlng`，缺少起点时再获取 `latlng` 坐标流；读写共用现有 OAuth 授权和令牌自动刷新。接口与权限见 [Strava 官方文档](https://developers.strava.com/docs/reference/)。

## 注意

- `strava_config.sh` 和 `.strava_tokens.json` 含敏感信息，已 gitignore，勿提交。
- Strava 访问令牌 6 小时过期，脚本自动用 refresh_token 刷新。
- 令牌刷新失效时会自动重新开浏览器授权。
- 两个脚本会将现有凭据文件权限收紧为 `600`，拒绝符号链接和硬链接；新 token 通过私有临时文件原子替换。
- Strava 网络请求中的密钥、token 和授权码通过标准输入传递。脚本在读取凭据前关闭 Shell 跟踪。优先使用 `login` 完成授权。
- 更新所用临时文件放在权限为 `700` 的随机目录内，文件权限为 `600`，结束时自动清理。
- `DRY_RUN=1` 始终禁止活动写入，包括同时设置 `YES=1` 的情况，两个脚本均适用。
- 处理记录与运行日志也含个人活动信息，已 gitignore，勿提交。
