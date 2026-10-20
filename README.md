# update-strava-weather

给 Strava 活动追加 gearaut 风格的天气标注（标题加天气 emoji，描述加温度/体感/湿度/风速块）。

默认为未处理活动补齐缺失天气，并根据轨迹与离线库生成山峰行，保留手写备注。旧 summitbag 数据行会清理；强制重算时替换可识别的旧天气块。更新基于读取到的活动内容，仅提交变化字段。

## 特性

- 一站式：OAuth 授权 / 令牌自动刷新 / 拉取活动 / 追加天气 / 更新
- `sync` 分页发现新增活动并重试积压任务；已有处理记录的 ID 不重新入队，需要 `--force` 才重新处理
- 标题：默认在原标题后追加天气 emoji，可自定义排版与固定文案或关闭更新；emoji 按 weather_code、当地时段和极值生成（🌞⛅☁️🌫️🌧️❄️⛈️🌙），体感 >30 加 🥵、<0 加 🥶，风速 >30 km/h 加 💨
- 描述：活动时段内各字段的 min - max（升序），默认模板保留 2 位小数
- 独立文本模板：自定义天气描述的字段、排版、精度和署名，默认输出保持原样
- 山峰匹配：默认用全部有效轨迹点匹配离线 OSM 山峰库，将结果放在天气前，可配置关闭
- 本地 API 用量统计及批量配额预留，优先为单活动同步保留额度
- 数据源：Open-Meteo（无需密钥；近期活动用 15 分钟 `minutely_15`，历史活动用 archive hourly）
- 支持预览（`sync --dry-run` / `show`）；`sync` 默认直接写入，手动更新默认交互确认
- 附带共享 Agent 技能，Claude 入口通过软链复用

## 目录结构

```
update-strava-weather/
├── update_strava_weather.sh        主脚本
├── strava_common.sh               凭据权限和安全请求公共逻辑
├── strava_oauth.py                本地 OAuth 回调与 state 校验
├── strava_sync.py                 自动补齐天气、处理记录与并发锁
├── weather_template.py            天气模板渲染、识别与替换
├── peaks.py                       轨迹山峰匹配与山峰行替换
├── peaks/                         离线 SQLite 库（不提交）及准备说明
├── api_usage.py                   本地 API 用量与配额预留检查
├── config.default.json            全部默认配置，含天气模板（提交）
├── config.local.json              个人配置与密钥，只填覆盖项（gitignore）
├── project_config.py              共享加载、校验和旧配置迁移
├── .strava_tokens.json             令牌缓存（gitignore，自动生成）
├── .strava_weather_state.json      扫描进度、处理记录和待处理队列（gitignore，自动生成）
├── .strava_api_usage.json          本地 API 用量记录（gitignore，自动生成）
├── AGENTS.md                      共享中文贡献指南
├── CLAUDE.md -> AGENTS.md          Claude 规则入口
├── .agents/skills/strava-weather-update/SKILL.md   共享技能源文件
├── .claude/skills -> ../.agents/skills             Claude 技能兼容入口
├── webhook/                       接收器、部署模板及独立部署说明
├── tests/                         隔离回归测试
├── .gitignore
└── README.md
```

## 首次配置

运行环境需要 Bash、Python 3.9+ 和 curl。

1. 到 https://www.strava.com/settings/api 注册应用，拿到 Client ID / Client Secret。
2. 把应用的 **Authorization Callback Domain** 设为 `localhost`。
3. 新建 `config.local.json` 并填入自己的应用密钥；已有旧配置时先按下文迁移：
   ```json
   {
     "strava": {
       "client_id": "你的 Client ID",
       "client_secret": "你的 Client Secret"
     }
   }
   ```
   运行 `chmod 600 config.local.json` 收紧权限。
4. 授权（首次）：
   ```bash
   ./update_strava_weather.sh login
   ```
   会自动开浏览器授权，回调到本地服务自动换令牌并缓存。授权 scope 含 `activity:read_all,activity:write`；回调校验随机 `state`，默认最多等待 5 分钟。

启用默认山峰功能前，按 [离线山峰库说明](peaks/README.md) 准备数据库；仓库不附带数据库，也不会自动下载。缺库时仍可同步天气，但山峰结果为空，可能清理原描述中的旧山峰数据行。

## 配置

所有默认值集中在 [config.default.json](config.default.json)，个人覆盖统一写入根目录 `config.local.json`（已 gitignore）。没有个人配置时直接使用默认值，不生成完整副本；只覆盖填写的字段，嵌套对象逐项合并，数组整体替换。真实 Strava 操作仍需提供自己的应用密钥并完成授权。

| 配置组 | 内容 |
| --- | --- |
| `strava` | 应用密钥、OAuth 等待超时、列表默认数量及令牌提前刷新时间 |
| `title` | 标题模板 `template`、标题更新开关 `enabled` |
| `weather` | 模板正文 `template`、可选外部模板路径 `template_file`、山峰匹配开关 `reorder_summitbag`（沿用旧名称） |
| `sync` | 分页大小、首次扫描及增量重叠天数 |
| `webhook` | 验证令牌、同步延迟、监听地址及端口、回调路径、个人 Hooks |
| `ngrok` | 域名、可执行文件路径、ngrok 自身配置文件路径 |
| `http` | 连接、Strava 请求及天气请求超时 |
| `rate_limit` | read / overall 的每日、15 分钟额度与批量操作预留，见下文配额说明 |
| `backfill` | 历史补扫轮数及重试等待参数 |
| `reorder` | 旧版独立文本排序脚本的分页、限流及请求间隔 |

优先级：命令行参数 > 对应环境变量 > `config.local.json` > `config.default.json`。保留的配置环境变量为 `CLIENT_ID`、`CLIENT_SECRET`、`WEATHER_TEMPLATE_FILE`、`REORDER_SUMMITBAG`、`WEBHOOK_SYNC_DELAY_SECONDS`、`NGROK_DOMAIN`；其它字段通过 JSON 修改。`false`、`0` 和空数组不会被当作缺失。无效 JSON、未知字段和错误类型会报错，不会静默回退；可运行 `python3 project_config.py check` 离线校验，不输出配置值。

旧版本升级时运行一次：

```bash
python3 project_config.py migrate
```

迁移会合并旧 `strava_config.sh`、`webhook/config.sh`、`webhook/receiver_config.json`，将其引用的天气模板正文一并纳入新文件；已有新配置字段优先，保留验证令牌和个人 Hooks。原文件不删除，但运行入口只读取新配置；确认迁移后可自行归档旧文件。令牌缓存、同步状态和日志属于运行数据，继续独立存放；ngrok 的 authtoken 仍由其自身私有配置管理。

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

# 补扫全部历史；已有处理记录的 ID 跳过，已有天气不重算
./update_strava_weather.sh sync --all

# 指定活动时不查列表；无 GPS 时可同时提供经纬度
./update_strava_weather.sh sync --activity-id 123 --latitude 39.9 --longitude 116.4
```

`sync` 默认直接补齐缺失天气。首次扫描最近 7 天；之后从“上次完整扫描时间往前 7 天”查到本轮开始时刻，覆盖停用期间。每页默认 50 条，可用 `--page-size` 调整为 1–200，完整分页扫描并去重。已有处理记录的 ID 不重新入队，跳过详情、轨迹和天气查询；此规则也适用于 `--all`、指定 ID 和重复 webhook 创建事件。未处理但已有天气的活动只更新山峰，不重算天气和标题；室内及 Virtual 活动跳过。

普通单条失败保留队列并继续其它活动，下次默认同步仍会重试范围外的积压；活动处理阶段上抛的 401、403 或 429 会停止本轮。活动返回 404 时记为 `gone` 并移出队列，不反复重试；404 也可能表示无访问权限，不能据此断言活动已删除。部分失败返回非零退出码。山峰轨迹获取错误会警告并降级为空山峰结果，包括该路径上的授权和限流错误；天气及写回流程仍可能继续。

分页失败或扫描中断不推进扫描进度，已发现任务保留在队列；再次运行默认 `sync` 会覆盖原范围。`--dry-run` 或 `DRY_RUN=1` 会读取真实 API，但不写活动、队列、处理结果或扫描进度；请求仍消耗 API 配额，并更新本地用量记录。

活动时间早于重叠范围的补传活动可能无法被默认扫描发现，请用 `--since` / `--until` 或 `--all` 补扫。显式日期范围和单个 ID 只处理所选活动，不处理范围外的积压，也不改变默认扫描进度；历史补扫分页失败后应重跑同一补扫命令。`--force` 重算本次查询范围内的活动，其强制重算标记会随失败任务保留到下次重试。

每条活动详情只读取一次，后续判断、天气计算和描述合并共用这份快照。需要补天气时使用起点坐标（缺少时获取轨迹流）。默认参与山峰匹配的运动还会请求轨迹：有起点且需写回时，通常为详情 GET + 轨迹 GET + PUT；缺起点时，天气定位可能再请求一次轨迹。不含列表分页、令牌刷新及 Open-Meteo 请求。只提交实际变化字段：仅更新山峰时只写 `description`，标题变化时才提交 `name`，全部无变化时不 PUT。本地同步锁避免本脚本并行写入，外部工具在读取后修改同一字段仍可能被覆盖。经纬度覆盖只允许配合 `--activity-id`，只用于天气定位，不替代山峰匹配的完整轨迹。

旧版处理记录自动迁移并保留完成结果，同一仓库同时只能运行一个 `sync`。手动删除天气后请指定 ID 加 `--force` 重算。记录损坏时会报错，不自动清空；如需重建，先停止同步、私有备份再移走记录并重新补扫，原队列和扫描进度不会自动恢复。

天气取值沿用活动开始时间加 `moving_time` 的区间，包含区间内采样点及首尾插值。emoji 的夜间判断使用 `start_date_local` 的当地钟表时间。Open-Meteo 请求使用 UTC 时间戳，近期活动用 forecast `minutely_15`，超过 5 天用 archive `hourly`，不会对缺失数据外推。

需要定时运行时，可自行将下例加入 crontab（替换仓库路径；此仓库不会自动安装定时任务）：

```cron
*/5 * * * * umask 077; cd /path/to/update-strava-weather && /bin/bash ./update_strava_weather.sh sync >> .strava_weather_sync.log 2>&1
```

请先手动运行 `login` 和 `sync --dry-run` 验证环境。定时任务的 PATH 中需有 `python3` 和 `curl`；授权失效时按命令提示手动重新登录。日志包含活动标题和描述，已加入 gitignore。

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

- `sync` — 批量增量同步或历史补扫；常用选项有 `--activity-id`、`--since` / `--until`、`--all`、`--force`、`--dry-run`、`--template` 和 `--reorder-summitbag` / `--no-reorder-summitbag`。配置完成后运行 `./update_strava_weather.sh sync --help` 查看完整参数
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
- `REORDER_SUMMITBAG` 默认 `1`，开启山峰匹配与山峰行替换（旧名称）；设为 `0` 关闭
- `DRY_RUN=1` 仅预览
- `YES=1` 跳过确认

## 运行模式

**模式一：定时任务（独立运行，开箱即用）**
适合只想"每隔一段时间扫一遍新活动"的用户，不需要公网回调。
用 cron / systemd timer 定时跑 `./update_strava_weather.sh sync`，配置示例见上文；历史补扫用 `./backfill_all.sh`。
命令会输出处理日志，失败返回非零退出码；配额预留导致的暂停可能返回 0，监控还应检查 `⏸️` 提示与待处理数量。轮询频率应结合活动量、轨迹请求和应用实际 API 配额设置。

**模式二：webhook 实时推送（需要公网回调）**
Strava 有新活动时官方把事件推送到你的回调地址，
`webhook/receiver.py` 收到创建事件后默认等 60 秒再触发单活动同步。配置、部署与排障见 [webhook 说明](webhook/README.md)。
两种模式可并存（webhook 为主、定时为兜底），也可只选其一。

## 标题模板

默认配置为 `title.enabled: true`、`title.template: "{title} {emoji}"`，保持现有「原标题 + 空格 + 天气 emoji」格式与旧天气标记清理规则。用户在已有 `config.local.json` 中合入覆盖项即可，例如将 emoji 放到前面并加固定文案：

```json
{
  "title": {
    "template": "{emoji} | {title} · 跑步"
  }
}
```

例如 `晨跑` 会生成 `🌧️🥵 | 晨跑 · 跑步`。`{title}` 表示清理旧天气标记后的原标题，必须出现一次；`{emoji}` 为计算出的天气符号，最多出现一次，也可以省略。模板须为非空单行文本，字面量花括号使用 `{{` / `}}`，不支持其它字段、表达式、转换或格式说明。错误模板在请求 API 前报错。

设置 `"title": {"enabled": false}` 可完整保留当前标题，包括原有 emoji；仅设置 `"template": "{title}"` 则仍会清理末尾旧天气标记。同一模板重复生成不会叠加固定前后缀；更换已应用的自定义前后缀时，应先清理标题中的旧文案，当前只识别当前模板和原有末尾天气标记。

自动同步、webhook 和手动更新共用配置。仅更新山峰时保持标题不变；已有天气或处理记录的活动需要显式重算才能应用新模板，建议先预览（将 `123` 替换为实际 ID）：

```bash
./update_strava_weather.sh sync --activity-id 123 --force --dry-run
```

确认输出后移除 `--dry-run` 执行。默认配置保留在 `config.default.json`，个人修改只写 `config.local.json`。

## 天气块模板

默认模板位于 `config.default.json` 的 `weather.template`，保留原有温度、体感、湿度、风速字段，以及两位小数、空格、换行和署名。

自定义时只需在 `config.local.json` 中合入 `weather.template`，换行用 JSON 的 `\n` 表示，例如：

```json
{
  "weather": {
    "template": "{emoji} 本次运动天气\n🌡️ {t_min:.1f}～{t_max:.1f}°C，体感 {at_min:.1f}～{at_max:.1f}°C\nNot Powered By www.gearaut.com 🍚"
  }
}
```

| 字段 | 含义与单位 |
| --- | --- |
| `{emoji}` | 现有规则计算的天气 emoji，与标题模板共用计算结果 |
| `{t_min}` / `{t_max}` | 最低／最高温度，°C |
| `{at_min}` / `{at_max}` | 最低／最高体感温度，°C |
| `{h_min}` / `{h_max}` | 最低／最高湿度，% |
| `{w_min}` / `{w_max}` | 最低／最高风速，km/h |

数值默认保留两位小数，可使用 `.0f` 至 `.6f` 指定精度；单位和文案由模板控制，删除某行即可隐藏相应字段。模板须为 UTF-8，包含固定文案以识别天气块。字面量花括号写成 `{{` 和 `}}`。不支持表达式、属性访问或条件语法；模板不存在、为空或字段错误时，`sync` 在请求 API 和写入处理记录之前报错。

手动运行、cron 和 webhook 共用该模板。已有独立模板文件时，也可设置 `weather.template_file`，或临时指定并预览（`123` 替换为实际活动 ID）：

```bash
./update_strava_weather.sh sync --activity-id 123 --force --dry-run --template weather.custom.txt
```

模板选择顺序为 `--template` > 环境变量 `WEATHER_TEMPLATE_FILE` > `weather.template_file` > `weather.template`；路径为空时使用正文。相对路径统一以仓库根目录为准。`WEATHER_DESC_FILE` 仍表示已经生成好的描述文本，不会解析其中的占位符。

**防重复与更新规则：** 描述包含 `www.gearaut.com`，或完整匹配当前模板的固定文案、字段位置与换行，即视为已有天气。数值和天气 emoji 按类型匹配，不要求等于本次计算结果；同时兼容历史 gearaut / QWeather 天气块。只有标题 emoji 时仍会补齐描述。每次同步基于本次读取的数据判断，失败重试时重新读取并计算。

默认同步跳过已处理 ID，未处理但已有天气的活动只更新山峰；更换模板后，可指定活动加 `--force` 更新天气。强制更新仅移除能定位的旧格式或当前模板天气块，保留前后备注；山峰行另按下述规则替换。若含域名但无法定位旧天气块，会报错而不追加。旧版 `reorder_summitbag.sh` 使用同一模板配置识别天气块。

建议个人模板保留 `www.gearaut.com` 署名，以便改版后仍能防重复。无署名的自定义模板只能按当前结构识别：修改固定文案或换行前，请保留旧模板，必要时手动清理旧天气块后再强制更新。

## 山峰匹配

默认开启，保留旧配置名 `weather.reorder_summitbag` 和命令行参数。`sync` 用完整 GPS 轨迹与本机 SQLite 山峰库匹配；数据库准备、运动类型距离规则、重复经过计数及输出格式统一见 [离线山峰库说明](peaks/README.md)。不根据活动标题猜山名，也不再保留 summitbag 数据作为结果。

输出顺序为「天气前的手写备注 → 新山峰行 → 天气」，例如以下为格式示意，山名与数值需以实际匹配结果为准：

```text
今天手写的路线记录
⛰️ 大阳台 (587 m) ×2 • ⛰️ 小阳台 (450 m)
温度: 13.58°C - 21.48°C 体感温度: 13.60°C - 22.50°C
湿度: 69% - 89% 风速: 2.98 km/h - 3.95 km/h
Powered By www.gearaut.com
```

旧 summitbag 链接、年度爬升行及以山峰 emoji 开头并带括号海拔的行会整行删除，再插入新结果；空结果也会删除旧行。缺库、无匹配、轨迹不足或轨迹获取失败均可能产生空结果，不代表保留旧数据。普通的 `⛰️ 今天的手写游记` 不作为山峰数据；其它备注和天气正文保留，空行可能规范化。没有可识别天气块时，新山峰行追加到剩余描述后。

已有天气且尚无处理记录时，只更新山峰，保留天气与标题。已有处理记录的活动不会再次检查山峰；确需刷新时使用下例，**`--force` 同时重算天气与标题**：

```bash
./update_strava_weather.sh sync --activity-id 123 --force --dry-run

# 本轮关闭山峰匹配与山峰行修改
./update_strava_weather.sh sync --no-reorder-summitbag
```

长期关闭可在 `config.local.json` 中合入 `"weather": {"reorder_summitbag": false}`，临时环境覆盖为 `REORDER_SUMMITBAG=0`；命令行优先级最高。

低层手动天气更新入口不会计算山峰，使用调用方提供的 `PEAK_LINE`；默认空值在开启山峰功能时也会清理旧山峰行。保留的 `reorder_summitbag.sh` 是独立旧命令：只处理 `TrailRun`，将原有山峰文本移到天气前，不调用离线库，和当前 `sync` 的匹配行为不同；同一开关也控制此命令。

## API 用量与配额

`strava_common.sh` 的数据 API 请求会尽力记录到 `.strava_api_usage.json`；GET 计入 read 和 overall，其它方法只计 overall。它是本仓库的本地估计，不是 Strava 账户的权威计数；其它应用、网页操作及旧版独立排序脚本的请求不自动计入，记录失败也不阻止请求。

```bash
python3 api_usage.py status    # 查看本地用量、预留与可用量；可能创建本地用量文件
python3 api_usage.py check     # 批量预留检查：通过返回 0，触线返回 1
```

额度及预留值统一配置在 `config.default.json` 的 `rate_limit`，按应用实际额度写入个人覆盖。日计数按 UTC 午夜（北京时间 08:00）重置，短窗口使用滚动 15 分钟估算。

未指定 `--activity-id` 的批量 `sync`（含日期范围和 `--all`）在列表发现完成后、以及每条活动前检查预留线；不是每次请求前检查，也不预先保证整条活动的请求预算。触线输出 `⏸️`、保留队列，本轮无其它失败时返回 0。所有单 ID 同步均豁免此检查，包括 webhook 和手动调用，但仍受 Strava 实际限流约束。

看到暂停应等相应窗口恢复后再运行，不能把退出码 0 当作全部处理完毕，也不要拆成单 ID 绕过保护。`bash backfill_all.sh` 的退出码 0 且队列非空分支会继续下一轮，并未针对预留暂停等待；遇到此情况应停止循环，等额度恢复后再补扫。实际 HTTP 429 的退避是另一条错误路径。

接口研究资料见 [官方 PUT 历史实测](docs/put-activity-api.md) 与 [网页表单分析](docs/web-activity-api.md)。当前天气同步仅更新 `name` / `description`，这些资料不代表同步已支持其它字段，也不授权额外修改活动。

## AI Skill

实际活动同步使用 [strava-weather-update 技能](.agents/skills/strava-weather-update/SKILL.md)，例如在仓库目录内要求 Agent“更新我最新活动的天气”。技能调用本地脚本并报告结果，无需连接 Strava MCP。

修改仓库前阅读 [AGENTS.md](AGENTS.md)，其中包含任务入口、验证命令和共享软链维护约定；webhook 运维另见 [部署说明](webhook/README.md)。

## 注意

- `config.local.json` 和 `.strava_tokens.json` 含敏感信息，已 gitignore，勿提交。旧配置也继续忽略。
- 脚本按令牌过期时间自动刷新；刷新失败时，`sync` 非交互退出并提示运行 `login`，其它交互命令会尝试浏览器重新授权。
- 两个脚本会将现有凭据文件权限收紧为 `600`，拒绝符号链接和硬链接；新 token 通过私有临时文件原子替换。
- Strava 网络请求中的密钥、token 和授权码通过标准输入传递。脚本在读取凭据前关闭 Shell 跟踪。优先使用 `login` 完成授权。
- 更新所用临时文件放在权限为 `700` 的随机目录内，文件权限为 `600`，结束时自动清理。
- `DRY_RUN=1` 始终禁止活动写入，包括同时设置 `YES=1` 的情况，两个脚本均适用。
- 处理记录与运行日志也含个人活动信息，已 gitignore，勿提交。
