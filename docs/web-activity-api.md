# Strava 网页版活动修改接口（内部接口文档）

> 推导来源：2026-10-17 对 Strava 网页版活动编辑页
>（`https://www.strava.com/activities/{id}/edit`）HTML 源码的只读分析。
> 这些是 Strava 内部接口，未在公开 API 文档
>（https://developers.strava.com/docs/reference/）中开放，
> 随时可能变更。使用前请先验证。

## 网页表单字段与公开 API 覆盖范围

本页来自 2026-10-17 的编辑页结构分析，记录表单线索，不代表所有字段均做过写回验证。后续 2026-10-19 的 [官方 PUT 实测](put-activity-api.md) 已确认 `private_note` 和 `selected_polyline_style` 也可通过公开 API 写入，不能把“公开文档未列出”理解为“公开 API 不接受”。当前同步更新标题、描述与地图样式，不使用网页接口。

以下为当时观察到的网页字段及功能：

| 字段 | 说明 |
|---|---|
| `activity[visibility]` | 可见性：`everyone` / `followers_only` / `only_me` |
| `activity[private_note]` | 仅自己可见的备注 |
| `activity[perceived_exertion]` | 主观用力程度（滑块值） |
| `activity[prefer_perceived_exertion]` | 用主观用力代替心率的开关 |
| `activity[selected_polyline_style]` | 地图样式（值列表见官方 PUT 实测） |
| `activity[stats_visibility][*]` | 按项隐藏数据：`start_time`、`calories`、`heart_rate`、`pace`、`speed`、`power`（每项 `everyone` / `only_me`） |
| `activity[bike_id]` / `activity[athlete_gear_id]` | 自行车 / 跑鞋（按运动类型二选一） |
| 标签（race、workout、long_run、commute 等） | 由 React 组件提交 |
| 修剪轨迹（truncate） | 本页未记录对应的公开 API 路径 |

## 认证

- **方式**：Strava 网页登录 session cookie（不是 OAuth token）。
- **CSRF**：每次 `GET` 编辑页现抓 `authenticity_token`
  隐藏字段的值（每次渲染都不同，不可复用）。
- 当次页面源码未发现 `<meta name="csrf-token">`，示例分析以表单内隐藏字段为准。

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
| `activity[selected_polyline_style]` | 否 | 地图样式，见下方链接 |
| `activity[perceived_exertion]` | 否 | 主观用力程度 |
| `activity[bike_id]` | 否 | 自行车 ID（骑行类） |
| `activity[athlete_gear_id]` | 否 | 跑鞋 ID（跑步类） |

### 地图样式与请求构造

值列表集中维护在 [官方 PUT 实测的地图样式表](put-activity-api.md#selected_polyline_style-可选值)，本页不重复复制。

只有用户要求操作网页接口时才准备网页登录凭据；它与仓库 OAuth token 不通用。cookie 文件应放在仓库外的私有目录，权限为 `600`，不要在聊天、日志或进程参数中传递 cookie 内容。

获取编辑页后，用 HTML 解析器读取隐藏的 `authenticity_token` 并确认非空，再用标准库 `urllib.parse.urlencode` 编码表单。将编码结果通过标准输入（`curl --data-binary @-`）或权限为 `600` 的临时文件提交；cookie 通过 `curl -b /path/to/private-cookie-file` 读取。不要把 token 展开成 `curl -d "authenticity_token=$TOKEN"`，否则会暴露到进程参数。

例如已获准的标题及地图样式修改包含以下表单键；这里是结构示意，不是可直接提交的有效表单：

```text
_method=patch
utf8=✓
authenticity_token=<当次编辑页的隐藏字段>
activity[name]=晨间跑步 🌧️
activity[selected_polyline_style]=elevation
```

提交后通过独立读路径验证效果，不因 HTTP 成功就认定所有字段已生效。本页不提供创建、修剪或批量修改真实活动的自动化流程。

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

## 注意事项

1. 内部接口无版本承诺，Strava 改版可能随时失效；用前先 GET 验证。
2. session cookie 有有效期，过期需重新登录。
3. `authenticity_token` 每次渲染都不同，必须先 GET 后 POST，不可缓存复用。
4. 仓库的本地 API 计数不覆盖网页请求；不能据此判断网页服务的限制，也不能用网页接口绕过配额。
