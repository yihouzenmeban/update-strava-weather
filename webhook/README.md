# Strava webhook 部署与运维

Strava 新活动产生时，官方 webhook 把事件推送到你的公网回调地址，
`receiver.py` 收到后默认等 60 秒（可配置，让 Strava 及其它工具先完成活动处理），
再触发 `update_strava_weather.sh sync --activity-id <id>`。同步行为、模板和山峰排序
统一见 [根目录 README](../README.md#用法)；本页说明事件、部署、延迟及个人 Hooks。

接收器只触发 `activity/create`，`activity/update` 和 `activity/delete` 均仅回复 200。
创建事件处理完成后才追加的山峰数据，依靠定时 `sync` 再次扫描，或手动指定活动 ID 同步。

## 文件

- `receiver.py` — webhook 接收服务（监听 127.0.0.1:8787）
  - `GET /strava-webhook`：Strava 订阅验证（hub.challenge 握手）
  - `POST /strava-webhook`：activity/create 事件 → 配置的延迟后触发单活动同步（默认 60 秒）
  - `GET /health`：存活探针
- `systemd/` — 两个 unit 模板（`__HOOK_DIR__` / `__NGROK_DOMAIN__`
  由 `ensure.sh` 安装时替换）
- `ensure.sh` — 幂等安装/恢复脚本；写入 systemd unit、启用服务并检查健康端点
- `config.sh.example` — 复制为 `config.sh` 并填你的 ngrok 保留域名

## 部署

先按 [首次配置](../README.md#首次配置) 完成 Strava 授权。本节针对 Linux + systemd + ngrok；仓库中的模板以 root 运行，接收器使用 `/usr/bin/python3`，隧道使用 `/home/hatch/bin/ngrok` 及 `/home/hatch/.config/ngrok/ngrok.yml`。其它机器安装前，先按实际路径调整 `webhook/systemd/` 模板；`ensure.sh` 不安装 ngrok，也不创建其授权配置。

1. 安装 ngrok，从 [ngrok dashboard](https://dashboard.ngrok.com) 获取 authtoken，配置到隧道模板指定的 ngrok 配置文件，权限设为 `600`。将保留域名填入 `webhook/config.sh`，只填域名，不带 `https://`。

   ```bash
   cp webhook/config.sh.example webhook/config.sh
   chmod 600 webhook/config.sh
   # 编辑 webhook/config.sh，填写 NGROK_DOMAIN
   ```

2. 确认服务可直接联网。仅在 Muse 环境中，需要打开「直接网络协议 → 其他 TCP 连接」；隧道 unit 会清除代理环境变量。
3. 在仓库根目录运行 `sudo bash webhook/ensure.sh`，检查本地及公网 `/health` 输出。若 VM 替换或临时系统目录重置导致 unit 丢失，可重跑恢复。模板变更后重跑会重新生成 unit；已有服务还需执行 `sudo systemctl restart strava-webhook-receiver.service strava-webhook-tunnel.service` 才会使用新的启动配置。
4. 注册 push subscription（由部署者使用自己的应用凭据操作）：
   `POST https://www.strava.com/api/v3/push_subscriptions`
   参数：`client_id`、`client_secret`、
   `callback_url=https://你的域名/strava-webhook`、
   `verify_token=`（`webhook/receiver_config.json` 里自动生成的）。
   再 `GET /push_subscriptions` 确认。

凭据通过私有配置或标准输入提供，勿写入命令行参数和日志。配置文件及日志不提交仓库。

## 触发延迟

默认 60 秒。可在已有的 `webhook/receiver_config.json` 中增加或修改 `"sync_delay_seconds": 120`，
保留原来的 `verify_token`；旧配置未设置此字段时仍使用 60 秒。新生成的配置会包含默认值。

也可通过接收器进程的环境变量 `WEBHOOK_SYNC_DELAY_SECONDS=120` 覆盖文件配置。
优先级为环境变量 > 配置文件 > 60 秒；取值必须是非负整数秒，`0` 表示不等待。
无效配置会阻止接收器启动并报错。

systemd 部署时，使用 `sudo systemctl edit strava-webhook-receiver.service` 创建覆盖配置，避免重跑 `ensure.sh` 覆盖直接编辑的生成 unit：

```ini
[Service]
Environment=WEBHOOK_SYNC_DELAY_SECONDS=120
```

修改 JSON 后重启接收器生效；修改 systemd 环境配置后先 reload 再重启：

```bash
sudo systemctl daemon-reload
sudo systemctl restart strava-webhook-receiver.service
```

延迟用于降低其它工具尚未完成描述修改的概率；它不提供跨工具的原子更新保证。
同步只使用延迟结束后的一次详情读取，失败重试时再重新读取，不固定追加第二次 GET。

## 个人 Hooks（可选）

`activity/create` 事件同时启动延迟天气同步和 `webhook/receiver_config.json` 中的个人 Hooks。Hooks 立即启动，不等待天气同步，也不受 `sync_delay_seconds` 控制。它们是本机扩展，不属于本仓库。

在已有配置中增加 `hooks`，保留真实 `verify_token`；下面的路径和令牌均为占位值，须替换，勿直接覆盖现有配置：

```json
{
  "verify_token": "替换为现有 verify_token",
  "sync_delay_seconds": 60,
  "hooks": [
    {"name": "personal-sync",
     "command": ["/path/to/personal-sync.sh"],
     "cwd": "/path/to/workdir"}
  ]
}
```

- `command` 必须是字符串数组（不经过 shell）；`cwd` 可选。
- 每个 hook 以 detached 子进程运行，不阻塞 200 响应；输出记到
  `webhook/logs/hook-<name>-<activity_id>.log`。
- 配置文件是 gitignored 的，按机器存放，仓库保持干净。

## 排障

先检查服务与本地端点，再检查公网端点（替换占位域名）：

```bash
systemctl status strava-webhook-receiver.service strava-webhook-tunnel.service
curl -q -fsS --max-time 10 http://127.0.0.1:8787/health
curl -q -fsS --max-time 15 https://YOUR_NGROK_DOMAIN/health
```

- 本地失败：查看 `webhook/logs/receiver.log`，检查 Python 路径、配置和目录访问权限。
- 本地正常、公网失败：查看 `webhook/logs/ngrok.log`，核对 ngrok 路径、授权配置、域名及直接网络访问。
- 已收到创建事件但未更新：延迟结束后查看 `webhook/logs/sync-<activity_id>.log`；根据授权、跳过或失败信息处理，必要时按活动 ID 手动同步。收到 HTTP 200 只代表事件已接收，不能据此认定后台同步成功。

## 本地模拟验证

在仓库根目录运行：

```bash
python3 tests/webhook.py
# 或
bash tests/regression.sh webhook
```

测试通过临时本机 HTTP 服务发送真实 POST，请求经过 `receiver.py`、后台子进程和真实的
`sync --activity-id` 流程；Strava / Open-Meteo 使用模拟响应，凭据、活动和处理记录全部位于
临时目录。测试替换 `sleep` 命令跳过实际等待，并核对传入的默认 60 秒、自定义 120 秒及 0 秒。

覆盖事件筛选、后台响应与失败重试、延迟配置，以及不同描述格式下的天气补齐和山峰排序；具体场景以 `tests/webhook.py` 为准。
