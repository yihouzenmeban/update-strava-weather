---
name: strava-weather-update
description: 通过本地 sync 命令批量为 Strava 活动补齐天气，分页发现新增活动、重试失败任务，并支持指定 ID 或日期的历史补扫。当用户说“更新 Strava 天气 / 给活动加天气 / 同步天气到 Strava”或要求补扫、强制重算时使用。
---

# Strava 活动天气标注

在仓库根目录运行本地命令。需要 Bash、Python 3、curl；首次配置 `strava_config.sh` 后用 `./update_strava_weather.sh login` 授予 `activity:read_all,activity:write`，Callback Domain 设为 `localhost`。无需 Strava MCP，不读取或输出配置密钥和 token。

## 默认流程

用户要求同步新增活动天气时，只执行一次：

```bash
./update_strava_weather.sh sync
```

不要先调用 `list`、`get`、`streams`，也不要自行查天气、计算范围或选择 emoji；这些步骤已由脚本完成。脚本默认直接写入缺失天气，无需额外确认。

- 首次查询最近 7 天；以后从上次完整扫描时间往前重叠 7 天，分页扫描到本轮开始时刻，按 ID 去重。不能只看第一条，也不遇到缓存就停止分页。
- 命中处理记录的 ID 不再请求详情和天气；发现的新 ID 每页先保存到队列，完整扫描后才推进进度。
- 未处理：读取详情；已有完整天气块或属于室内活动时跳过并记录。
- 缺少天气：优先用起点坐标，缺少时读取轨迹流；查询 Open-Meteo，计算活动区间内 min/max（含边界插值），生成 emoji 和两位小数描述。
- 写入前重新读取活动，保留最新备注和 summitbag；若其它工具已经补上天气则跳过。
- 每条成功更新或确认已有天气/室内活动后立即记录完成；普通单条失败保留队列并继续其它活动，后续默认同步仍会重试范围外的失败 ID。
- 分页失败不推进进度；中断后默认同步继续覆盖原查询范围。预览不写活动、处理记录、队列或扫描进度。

## 按用户意图选择参数

下列 `123` 为虚构 ID，必须替换为用户指定的 ID：

```bash
# 指定活动，不查最新列表
./update_strava_weather.sh sync --activity-id 123

# 用户要求先看结果
./update_strava_weather.sh sync --dry-run

# 用户指定日期补扫，日期按 UTC，包含起止当天
./update_strava_weather.sh sync --since 2026-10-01 --until 2026-10-15

# 用户要求补齐全部历史
./update_strava_weather.sh sync --all

# 仅在用户明确要求重算或修复被手动删除的天气时使用
./update_strava_weather.sh sync --activity-id 123 --force
```

`--activity-id`、`--since`、`--all` 互斥，`--until` 只能与 `--since` 配合；显式范围只处理所选活动，不推进默认扫描进度。`--force` 重算本次所选范围，不只重算最新一条，且仍跳过室内活动；失败时强制重算标记会保留。多个指定 ID 逐个运行并汇总结果，不额外扫描列表。

## 错误处理与结果

- 无 GPS：询问活动地点；获得可靠坐标后运行 `sync --activity-id ID --latitude LAT --longitude LNG`，不猜测常用地点。
- 授权失效：提示用户运行 `./update_strava_weather.sh login`；手动 `auth` 从标准输入读取授权码，不把凭据放进命令行参数。
- 授权失败或 429：停止本轮，报告原因和剩余队列；不连续重试。
- 普通单条失败：其它活动继续处理，报告成功数量、失败 ID 和原因；不要将部分失败说成全部成功。
- 历史补扫分页失败：提示重跑原补扫命令，不能声称未扫描的历史已经入队。
- 处理记录损坏：报告错误，不自动清空已有记录。
- 已处理、已有天气、室内或并发跳过：简短报告跳过原因即可。
- 更新成功：展示脚本输出的最终标题和天气描述。保留失败退出码，不声称已成功。

默认重叠范围之外的旧活动补传需要用户指定日期或 `--all` 补扫。脚本自动迁移旧缓存，不安装定时任务；需要定时运行时使用 README 的示例。日志和处理记录均只留本地。
