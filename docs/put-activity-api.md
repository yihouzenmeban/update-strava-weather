# Strava 官方 PUT /activities/{id} 接口实测文档

> 历史实测记录：2026-10-19 创建手动测试活动，逐字段 PUT 后通过 GET 或网页验证；测试活动已删除。下表中的“文档是否列出”指当时的公开文档，并非当前支持承诺。手动活动的距离、用时结果不能外推为所有 GPS 活动均可修改。

本页是接口研究资料；当前仓库同步只提交 `name` / `description`。阅读本页不意味着可以创建测试活动或修改其它字段，真实写入仍需处于用户授权范围。网页 session 与表单字段另见 [网页接口分析](web-activity-api.md)。

## 实测接受的字段

| 字段 | 文档是否列出 | 接受的值 | 说明 |
|---|---|---|---|
| `name` | 是 | 字符串 | 活动标题 |
| `description` | 是 | 字符串（支持换行） | 活动描述 |
| `commute` | 是 | `true` / `false` | 通勤标记 |
| `trainer` | 是 | `true` / `false` | 室内标记 |
| `hide_from_home` | 是 | `true` / `false` | 在首页静音 |
| `sport_type` | 是 | 有效运动类型（如 `Run`、`Ride`、`TrailRun`） | 非法值返回 400 `{"message":"Bad Request","errors":[]}` |
| `type` | 是（已废弃） | 有效运动类型 | 实测接受（`Run`→`Ride` 生效，`sport_type` 联动变化） |
| `gear_id` | 是 | 装备 ID | 用假 ID 测试无变化；需真实装备 ID，未完全验证 |
| `elapsed_time` | 否 | 整数（秒） | **文档未列出但实测接受**（手动活动 600→900 生效） |
| `distance` | 否 | 数字（米） | **文档未列出但实测接受**（手动活动 0→5000 生效） |
| `selected_polyline_style` | **否** | 15 个值（见下表） | **文档未列出但实测接受**，HTTP 200；GET/PUT 返回中均不回显该字段，当前值只能去网页编辑页看 |
| `private_note` | **否** | 字符串 | **文档未列出但实测接受**，仅自己可见的备注；PUT 后 GET 可查到 |

### selected_polyline_style 可选值

| value | 网页显示名 |
|---|---|
| `activity_shorts` | 活动回放 |
| `default` | 标准 |
| `fatmap_satellite_3d` | 3D |
| `winter_3d` | 冬季 3D |
| `surface_type` | 泥土 |
| `elevation` | 海拔 |
| `gradient` | 坡度 |
| `heartrate` | 心率 |
| `pace` | 配速 |
| `power` | 功率 |
| `speed` | 速度 |
| `temperature` | 温度 |
| `time` | 时间 |
| `heatmap` | 活动热力 |
| `metro` | Strava Metro |

## 实测不接受的字段（PUT 后 GET 无变化）

| 字段 | 测试值 | 结果 |
|---|---|---|
| `visibility` | `followers_only` | 无变化（仍为 `everyone`） |
| `private` | `true` | 无变化（仍为 `false`） |
| `perceived_exertion` | `7` / `activity[perceived_exertion]=8` | 无变化（仍为 `null`） |
| `prefer_perceived_exertion` | `true` | 无变化（仍为 `null`） |
| `start_date_local` | `2026-10-18T10:00:00` | 无变化（开始时间不可改） |
| `stats_visibility` | `stats_visibility[pace]=only_me` | PUT 返回 `{"message":"error"}`，值无变化 |

其余 GET 字段（`id`、`athlete`、`map`、`laps`、各类计数器、平均/最大配速心率等）
未逐个测试，不能仅凭 GET 返回就推断可写。

> 注意：未知字段不会报错，Strava 静默忽略。HTTP 200 不代表字段生效，
> 必须用独立的读路径（GET 或网页）验证。

## 读写不对称

- `selected_polyline_style`：当时 PUT 200 且网页确认生效，
  但该次 GET 和 PUT 响应均不回显。
  想知道当前值只能去网页编辑页看下拉框。
- `private_note`：写得进去，GET 也能查到，是例外。

## 示例

以下从仓库根目录使用 Bash 执行，需已有有效且未过期的令牌缓存；这个低层示例不自动刷新令牌。`123` 为虚构 ID，替换为明确获准修改的活动。共享请求函数把授权头经标准输入传给 curl，避免 token 出现在外部进程参数中；不要开启跟踪或输出变量。示例仅演示地图样式写入，仍须另行读取网页确认效果。

```bash
set +x
SCRIPT_DIR="$PWD"
source "$SCRIPT_DIR/strava_common.sh"
ACCESS=$(token_field access_token)
AID=123
strava_curl "$ACCESS" --fail -X PUT "https://www.strava.com/api/v3/activities/$AID" \
  --data-urlencode "selected_polyline_style=gradient" --output /dev/null
unset ACCESS
```
