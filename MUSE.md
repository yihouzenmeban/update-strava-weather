# 在 Muse 上部署 Strava 天气同步（含 webhook 实时推送）

这是一份给 Muse（Meta 的 AI 助手）看的部署 prompt。把这个仓库交到 Muse 手里，
让它按下面的步骤做，就能把"Strava 活动自动标注天气"完整跑起来。
人类用户需要准备的东西列在"开始之前"，其余都由 Muse 执行。

## 这个仓库是干什么的

- 每次 Strava 产生带 GPS 的户外活动（跑步、骑行、徒步、越野跑等；跳过室内和 Virtual
  活动），自动查活动当时的天气（Open-Meteo，按 GPS+时间），
  写回活动标题（加天气 emoji）和描述（天气块）。
- 两种运行模式，可并存：
  - **webhook 实时推送**（推荐）：Strava 有新活动即推送事件，约 1 分钟后自动标注。
  - **定时轮询**：cron 每 30 分钟跑一次 `sync`，补漏。
- `backfill_all.sh`：历史活动全量补扫（断点续传）。
- 个人扩展（如 Garmin→COROS 同步）通过 webhook hooks 接入，不进仓库。

## 开始之前（人类准备）

1. **Strava API 应用**：到 https://www.strava.com/settings/api 注册，
   拿到 Client ID / Client Secret；把应用的 **Authorization Callback Domain** 设为 `localhost`。
2. **ngrok**：到 https://dashboard.ngrok.com 拿 authtoken，并保留一个免费域名
   （如 `xxx.ngrok-free.dev`）。
3. **Muse App 设置**：打开「直接网络协议 → 其他 TCP 连接」，否则 ngrok 连不上
  （报错 `ERR_NGROK_9009`）。
4. 告诉 Muse 上面三样东西的值（Client ID/Secret、ngrok authtoken、保留域名）。
   凭据只走 Muse 的安全流程或写进 0600 权限文件，绝不在聊天里复述。

## 部署步骤（Muse 执行）

### 1. 克隆并配置

```bash
git clone https://github.com/yihouzenmeban/update-strava-weather.git
cd update-strava-weather
```

新建 `config.local.json`（已 gitignore，0600 权限），填入：

```json
{
  "strava": {
    "client_id": "你的 Client ID",
    "client_secret": "你的 Client Secret"
  },
  "ngrok": {
    "domain": "你的 ngrok 保留域名"
  }
}
```

```bash
chmod 600 config.local.json
python3 project_config.py check   # 校验配置，不输出敏感值
```

配置优先级：环境变量 > `config.local.json` > `config.default.json`。
所有可配置项见 `config.default.json`；webhook 相关在 `webhook` 段
（`verify_token`、`sync_delay_seconds` 默认 60、`listen_host`/`listen_port`/`path`、`hooks`）。

### 2. OAuth 授权（首次）

```bash
./update_strava_weather.sh login
```

会自动开浏览器走 Strava 授权，回调到本地服务换令牌并缓存。
scope 需要 `activity:read_all,activity:write`。令牌之后自动刷新。

先 dry-run 验证：

```bash
./update_strava_weather.sh sync --dry-run
```

### 3. ngrok 隧道

把 authtoken 写进 ngrok 配置（默认 `~/.config/ngrok/ngrok.yml`，0600）：

```yaml
version: "3"
agent:
  authtoken: 你的authtoken
```

> 注意：Muse 的 VM 上 ngrok 必须直连（不要走代理），否则免费版拒绝连接。
> systemd 模板里已带 `UnsetEnvironment` 摘掉代理变量。

### 4. 安装 webhook 服务

```bash
sudo bash webhook/ensure.sh
```

这个脚本是幂等的：从 `webhook/systemd/` 模板按 `config.local.json`
渲染 unit 文件，安装到 `/etc/systemd/system`，启动 receiver + ngrok 隧道，
并做健康检查。

- receiver 监听 `127.0.0.1:8787`（以 root 跑 systemd，路径锚定在文件自身，
  不要用 `Path.home()`）。
- 公网入口：`https://你的域名/strava-webhook`，ngrok 转发到本地 8787。

### 5. 注册 push subscription

从 `config.local.json` 的 `webhook.verify_token` 取值（首次启动 receiver
时自动生成并写回该文件，直接读这个 0600 文件即可）：

```bash
curl -X POST https://www.strava.com/api/v3/push_subscriptions \
  -d client_id=你的ID -d client_secret=你的Secret \
  -d callback_url=https://你的域名/strava-webhook \
  -d verify_token=你的verify_token
```

再 `GET /push_subscriptions` 确认注册成功（每个 Strava 应用只能有一个订阅）。

### 6. 端到端验证

1. 在 Strava App 里手动创建一个带 GPS 的跑步活动（或上传一个 GPX）。
2. 约 2 秒内 `webhook/logs/receiver.log` 应出现 `activity/create` 事件。
3. 约 60 秒后（可配置），`webhook/logs/sync-<活动ID>.log` 有同步记录；
   活动标题应加上天气 emoji，描述里有天气块。
4. 验证完后，**在 Strava App 里手动删除这条测试活动**
   （API 的 `write` 权限删不掉活动，只能手动删）。

### 7. 可选：历史补扫

```bash
chmod +x backfill_all.sh
./backfill_all.sh
```

断点续传，状态在 `.strava_weather_state.json`。注意 Strava 限流：
**read 档 2000/天**（不是 4000）——GET 密集型的补扫撞的是这一档；
撞 429 后脚本自动退避，必要时休眠到 UTC 午夜。

### 8. 可选：个人 hooks

想在每次新活动时触发自己的脚本（如同步到别的平台），在
`config.local.json` 的 `webhook.hooks` 里加（按机器存放，不进仓库）：

```json
{
  "webhook": {
    "hooks": [
      {"name": "my-sync",
       "command": ["/path/to/sync.sh", "--arg"],
       "cwd": "/path/to/workdir"}
    ]
  }
}
```

`command` 必须是字符串数组（不经过 shell）；每个 hook 以 detached 子进程运行，
不阻塞 200 响应；输出记到 `webhook/logs/hook-<name>-<活动ID>.log`。
改完重启 receiver 生效。

## 排障（都是实战踩过的坑）

| 现象 | 原因 / 处理 |
| --- | --- |
| ngrok 报 `ERR_NGROK_9009` | 没开 Muse App 的「直接网络协议 → 其他 TCP 连接」 |
| VM 重启后新活动不再自动标注 | `/etc/systemd/system` 是临时的，重启会丢 unit；重跑 `sudo bash webhook/ensure.sh` |
| `sync` 报 429，但配额明明没用完 | 撞的是 **2000/天 read 档**，不是 4000 overall 档；等 UTC 午夜重置 |
| 测试活动删不掉 | API 删不掉活动，去 App 里手动删 |
| receiver 起不来，报配置错误 | `python3 project_config.py check` 看哪项缺了；`verify_token` 为空会自动生成 |
| 日志里每行出现两次 | FileHandler 和 systemd 的 stdout 追加到同一个文件，无害 |

## 日常维护

- webhook 模式下不需要轮询；留一个低频 cron 做兜底即可。
- 升级：`git fetch origin && git reset --hard origin/main`，
  然后 `chmod +x backfill_all.sh webhook/ensure.sh`
  （经 API 推送的文件不保留可执行位）。
- 升级后若 receiver 有变化，复制 `webhook/receiver.py` 到部署目录并
  `sudo systemctl restart strava-webhook-receiver`。
