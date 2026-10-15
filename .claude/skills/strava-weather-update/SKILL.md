---
name: strava-weather-update
description: 通过本地 sync 命令为 Strava 活动补齐天气，自动跳过已处理和室内活动，生成 gearaut 风格标题与描述。当用户说“更新 Strava 天气 / 给活动加天气 / 同步天气到 Strava”或要求强制重算时使用。
---

# Strava 活动天气标注

在仓库根目录运行本地命令。需要 Bash、Python 3、curl；首次配置 `strava_config.sh` 后用 `./update_strava_weather.sh login` 授予 `activity:read_all,activity:write`，Callback Domain 设为 `localhost`。无需 Strava MCP，不读取或输出配置密钥和 token。

## 默认流程

用户要求为最新活动补天气时，只执行一次：

```bash
./update_strava_weather.sh sync
```

不要先调用 `list`、`get`、`streams`，也不要自行查天气、计算范围或选择 emoji；这些步骤已由脚本完成。脚本默认直接写入缺失天气，无需额外确认。

- 命中处理记录：只查一次活动列表，立即跳过详情和天气查询。
- 未处理：读取详情；已有完整天气块或属于室内活动时跳过并记录。
- 缺少天气：优先用起点坐标，缺少时读取轨迹流；查询 Open-Meteo，计算活动区间内 min/max（含边界插值），生成 emoji 和两位小数描述。
- 写入前重新读取活动，保留最新备注和 summitbag；若其它工具已经补上天气则跳过。
- 成功更新或确认已有天气/室内活动才记录；失败可重试，预览不写活动或处理记录。

## 按用户意图选择参数

下列 `123` 为虚构 ID，必须替换为用户指定的 ID：

```bash
# 指定活动，不查最新列表
./update_strava_weather.sh sync --activity-id 123

# 用户要求先看结果
./update_strava_weather.sh sync --dry-run

# 仅在用户明确要求重算或修复被手动删除的天气时使用
./update_strava_weather.sh sync --force
```

`--force` 可以与 `--activity-id`、`--dry-run` 组合，但仍跳过室内活动。多个指定 ID 逐个运行 `sync --activity-id ID` 并汇总结果，不额外扫描列表。

## 错误处理与结果

- 无 GPS：询问活动地点；获得可靠坐标后运行 `sync --activity-id ID --latitude LAT --longitude LNG`，不猜测常用地点。
- 授权失效：提示用户运行 `./update_strava_weather.sh login`；手动 `auth` 从标准输入读取授权码，不把凭据放进命令行参数。
- 429、网络失败、数据缺失或写入失败：报告原因；脚本不记录成功，不连续重试。
- 处理记录损坏：报告错误，不自动清空已有记录。
- 已处理、已有天气、室内或并发跳过：简短报告跳过原因即可。
- 更新成功：展示脚本输出的最终标题和天气描述。保留失败退出码，不声称已成功。

脚本只处理最新一次或指定的活动，不自动补扫历史，也不安装定时任务。需要定时运行时使用 README 的示例；日志、处理记录均只留本地。
