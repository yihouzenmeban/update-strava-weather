---
name: strava-weather-update
description: 通过本地 sync 命令为实际 Strava 活动补齐天气、整理山峰顺序、补扫历史或强制重算。当用户要求“更新 Strava 天气 / 给活动加天气 / 同步天气到 Strava”时使用；不用于修改项目代码、迁移 Agent 配置或仅解释实现。
---

# Strava 活动天气标注

在仓库根目录运行本地命令；首次配置见 [README 的首次配置](../../../README.md#首次配置)。无需 Strava MCP，不读取或输出配置密钥和 token。

## 默认流程

用户要求同步新增活动天气时，只执行一次：

```bash
./update_strava_weather.sh sync
```

不要先调用 `list`、`get`、`streams`，也不要自行查天气、计算范围或选择 emoji；这些步骤已由脚本完成。脚本默认直接写入缺失天气，无需额外确认。

默认同步补齐缺失天气并检查山峰顺序，保留手写备注和已有天气；室内活动跳过。扫描、计算、持久化和重试均交给脚本。范围与恢复语义见 [README 的用法](../../../README.md#用法)。

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

# 用户指定自定义描述模板；相对路径以仓库根目录为准
./update_strava_weather.sh sync --template weather.custom.txt

# 用户要求关闭山峰排序
./update_strava_weather.sh sync --no-reorder-summitbag
```

`--activity-id`、`--since`、`--all` 互斥，`--until` 只能与 `--since` 配合；显式范围不处理范围外积压或推进默认扫描进度。`--force` 重算整个所选范围，仍跳过室内活动，失败后重算标记会保留；不要擅自扩大范围。多个指定 ID 逐个运行并汇总，不额外扫描列表。`--dry-run` 会读取真实 API，但不写活动或同步状态。

## 错误处理与结果

- 无 GPS：询问活动地点；获得可靠坐标后运行 `sync --activity-id ID --latitude LAT --longitude LNG`，不猜测常用地点。
- 授权失效：提示用户运行 `./update_strava_weather.sh login`；手动 `auth` 从标准输入读取授权码，不把凭据放进命令行参数。
- 授权失败或 429：停止本轮，报告原因和剩余队列；不连续重试。
- 普通单条失败：其它活动继续处理，报告成功数量、失败 ID 和原因；不要将部分失败说成全部成功。
- 历史补扫分页失败：提示重跑原补扫命令，不能声称未扫描的历史已经入队。
- 处理记录损坏：报告错误，不自动清空已有记录。
- 已处理、已有天气、室内或并发跳过：简短报告跳过原因即可。
- 更新成功：展示脚本输出的最终标题和描述；仅排序时说明天气与标题未重算。保留失败退出码。

默认首次扫描最近 7 天，后续扫描覆盖停用期间并向前重叠 7 天；范围之外的旧活动补传需要指定日期或 `--all`。定时运行见 [README 的运行模式](../../../README.md#运行模式)，仅在用户要求时配置。日志和处理记录均只留本地。
