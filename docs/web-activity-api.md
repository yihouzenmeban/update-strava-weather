# Strava 活动修改接口文档

> 包含两部分：网页版内部接口（2026-10-17 由编辑页 HTML 源码只读推导），
> 以及官方 `PUT /activities/{id}` 的实测字段矩阵
>（2026-10-19 用测试活动逐字段 PUT+GET 验证）。
> 内部接口与未公开字段随时可能变更，使用前请先验证。

## 与公开 API 的差异

公开 API `PUT /activities/{id}` 文档中只列出 8 个字段
（`commute`、`trainer`、`hide_from_home`、`description`、`name`、
`type`（已废弃）、`sport_type`、`gear_id`），
但实测接受更多字段（见文末"附：官方 PUT 接口实测"）。

网页版额外支持（公开 API 没有）：

| 字段 | 说明 |
|---|---|
| `activity[visibility]` | 可见性：`everyone` / `followers_only` / `only_me` |
| `activity[private_note]` | 仅自己可见的备注 |
| `activity[perceived_exertion]` | 主观用力程度（滑块值） |
| `activity[prefer_perceived_exertion]` | 用主观用力代替心率的开关 |
| `activity[selected_polyline_style]` | 地图样式（见下表，共 15 个值）。实测官方 API 也接受该字段（`PUT /activities/{id}` 传 `selected_polyline_style=<value>`），无需走网页表单 |
| `activity[stats_visibility][*]` | 按项隐藏数据：`start_time`、`calories`、`heart_rate`、`pace`、`speed`、`power`（每项 `everyone` / `only_me`） |
| `activity[bike_id]` / `activity[athlete_gear_id]` | 自行车 / 跑鞋（按运动类型二选一） |
| 标签（race、workout、long_run、commute 等） | 由 React 组件提交 |
| 修剪轨迹（truncate） | 公开 API 完全没有对应端点 |

## 认证

- **方式**：Strava 网页登录 session cookie（不是 OAuth token）。
- **CSRF**：每次 `GET` 编辑页现抓 `authenticity_token`
  隐藏字段的值（每次渲染都不同，不可复用）。
- 页面源码中没有 `<meta name="csrf-token">`，token 只来自表单内隐藏字段。

## 接口一：更新活动

```
POST https://www.strava.com/activities/{id}
Content-Type: application/x-www-form-urlencoded
Cookie: <网页登录 session>
```

表单字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `utf8` | 是 | 固定值 `✓`（Rails 表单标记） |
| `_method` | 是 | 固定值 `patch`（Rails 方法重写，实际按 PATCH 处理） |
| `authenticity_token` | 是 | 当次编辑页渲染的 CSRF token |
| `activity[name]` | 否 | 活动标题 |
| `activity[description]` | 否 | 活动描述（支持换行） |
| `activity[sport_type]` | 否 | 如 `Run`、`Ride`、`Hike` |
| `activity[visibility]` | 否 | `everyone` / `followers_only` / `only_me` |
| `activity[private_note]` | 否 | 私人备注 |
| `activity[hide_from_home]` | 否 | `true` = 在首页设为静音 |
| `activity[selected_polyline_style]` | 否 | 地图样式，见下表 |
| `activity[perceived_exertion]` | 否 | 主观用力程度 |
| `activity[bike_id]` | 否 | 自行车 ID（骑行类） |
| `activity[athlete_gear_id]` | 否 | 跑鞋 ID（跑步类） |

### 地图样式可选值（`activity[selected_polyline_style]`）

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

### 示例

```bash
# 1. 从浏览器复制登录 cookie 存为 cookies.txt
# 2. 现抓 CSRF token
TOKEN=$(curl -s -b cookies.txt \
  https://www.strava.com/activities/123/edit \
  | grep -o 'name="authenticity_token" value="[^"]*"' | cut -d'"' -f4)

# 3. 提交（改标题 + 地图样式为海拔）
curl -b cookies.txt -c cookies.txt \
  -d "_method=patch" \
  -d "authenticity_token=$TOKEN" \
  -d "utf8=✓" \
  -d "activity[name]=晨间跑步 🌧️" \
  -d "activity[selected_polyline_style]=elevation" \
  https://www.strava.com/activities/123
```

## 接口二：修剪轨迹（Truncate）

```
POST https://www.strava.com/activities/{id}/truncate
Content-Type: application/x-www-form-urlencoded
Cookie: <网页登录 session>
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `authenticity_token` | 是 | 当次修剪页渲染的 CSRF token |
| `start_index` | 是 | 起始轨迹点索引（默认 0） |
| `end_index` | 是 | 结束轨迹点索引 |

> ⚠️ **不可撤销**：修剪会永久删除轨迹范围外的 GPS 点，
> 地图、距离、爬升都会重算。慎用。

## 附：官方 PUT /activities/{id} 实测字段矩阵

> 测试方法：2026-10-19 用官方 `POST /activities` 建一条手动测试活动
>（id 20642459412，标题"API字段测试"），对其逐个字段 `PUT` 后再 `GET`
> 验证是否生效。只看文档会漏东西——`selected_polyline_style` 和
> `private_note` 都不在文档中，但实测被接受。
> 测试活动需在 App 手动删除（官方 API 没有删除接口）。

### 实测接受的字段

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
| `selected_polyline_style` | **否** | 15 个值（见"地图样式可选值"表） | **文档未列出但实测接受**，HTTP 200；GET/PUT 返回中均不回显该字段，当前值只能去网页编辑页看 |
| `private_note` | **否** | 字符串 | **文档未列出但实测接受**，仅自己可见的备注；PUT 后 GET 可查到 |

### 实测不接受的字段（PUT 后 GET 无变化）

| 字段 | 测试值 | 结果 |
|---|---|---|
| `visibility` | `followers_only` | 无变化（仍为 `everyone`） |
| `private` | `true` | 无变化（仍为 `false`） |
| `perceived_exertion` | `7` / `activity[perceived_exertion]=8` | 无变化（仍为 `null`） |
| `prefer_perceived_exertion` | `true` | 无变化（仍为 `null`） |

> 注意：未知字段不会报错，Strava 静默忽略。HTTP 200 不代表字段生效，
> 必须用独立的读路径（GET 或网页）验证。

### 读写不对称

- `selected_polyline_style`：写得进去（PUT 200 且网页确认生效），
  但 `GET /activities/{id}` 返回的 70 个字段里没有它，PUT 返回的 72 个字段里也不回显。
  想知道当前值只能去网页编辑页看下拉框。
- `private_note`：写得进去，GET 也能查到，是例外。

### 示例

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

## 注意事项

1. 内部接口无版本承诺，Strava 改版可能随时失效；用前先 GET 验证。
2. session cookie 有有效期，过期需重新登录。
3. `authenticity_token` 每次渲染都不同，必须先 GET 后 POST，不可缓存复用。
4. 公开 API 的限流（2000/天 read 档等）不适用于这些网页接口，
   但频繁调用仍可能触发 Strava 的反自动化机制。
