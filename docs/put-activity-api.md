# Strava 官方 PUT /activities/{id} 接口实测文档

> 测试方法：2026-10-19 用官方 `POST /activities` 建一条手动测试活动
>（id 20642459412），对其逐个字段 `PUT` 后再 `GET` 验证是否生效。
> 只看文档会漏东西——实测发现文档未列出的字段也被接受（见下）。
> 测试活动需在 App 手动删除（官方 API 没有删除接口）。

## 实测接受的字段

| 字段 | 文档是否列出 | 接受的值 | 说明 |
|---|---|---|---|
| `name` | 是 | 字符串 | 活动标题 |
| `description` | 是 | 字符串（支持换行） | 活动描述 |
| `commute` | 是 | `true` / `false` | 通勤标记 |
| `trainer` | 是 | `true` / `false` | 室内标记 |
| `hide_from_home` | 是 | `true` / `false` | 在首页静音 |
| `sport_type` | 是 | 有效运动类型（如 `Run`、`Ride`、`TrailRun`） | 非法值返回 400 `{"message":"Bad Request","errors":[]}` |
| `type` | 是（已废弃） | 同 `sport_type` | 废弃字段，未实测 |
| `gear_id` | 是 | 装备 ID | 需真实装备 ID，未实测 |
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

> 注意：未知字段不会报错，Strava 静默忽略。HTTP 200 不代表字段生效，
> 必须用独立的读路径（GET 或网页）验证。

## 读写不对称

- `selected_polyline_style`：写得进去（PUT 200 且网页确认生效），
  但 `GET /activities/{id}` 返回的 70 个字段里没有它，PUT 返回的 72 个字段里也不回显。
  想知道当前值只能去网页编辑页看下拉框。
- `private_note`：写得进去，GET 也能查到，是例外。

## 示例

```bash
TOKEN=<access_token>
AID=20642459412

# 改地图样式为坡度（文档未列出但有效）
curl -X PUT "https://www.strava.com/api/v3/activities/$AID" \
  -H "Authorization: Bearer $TOKEN" \
  -d "selected_polyline_style=gradient"

# 写私人备注（文档未列出但有效）
curl -X PUT "https://www.strava.com/api/v3/activities/$AID" \
  -H "Authorization: Bearer $TOKEN" \
  -d "private_note=测试备注"
```
