# Strava webhook 实时推送（替代轮询）

Strava 新活动产生时，官方 webhook 把事件推送到你的公网回调地址，
`receiver.py` 收到后默认等 60 秒（可配置，让 Strava 及其它工具先完成活动处理），
再触发 `update_strava_weather.sh sync --activity-id <id>` 拉详情、
需要时查 Open-Meteo 天气、写回活动。起点坐标完整时，新补天气或仅调整山峰排序
使用 2 次 Strava API 调用（读一次详情、更新）；默认开启排序且无需修改时只读 1 次详情。
更新只提交变化的字段，仅排序时只写描述，手写备注保持在山峰及天气前面。
已处理活动也会按默认配置检查后来追加的山峰数据；可设置 `REORDER_SUMMITBAG=0` 关闭。

接收器只触发 `activity/create`，`activity/update` 和 `activity/delete` 均仅回复 200。
创建事件处理完成后才追加的山峰数据，依靠定时 `sync` 再次扫描，或手动指定活动 ID 同步。

## 文件

- `receiver.py` — webhook 接收服务（监听 127.0.0.1:8787）
  - `GET /strava-webhook`：Strava 订阅验证（hub.challenge 握手）
  - `POST /strava-webhook`：activity/create 事件 → 配置的延迟后触发单活动同步（默认 60 秒）
  - `GET /health`：存活探针
  - 注意：服务以 root 跑 systemd，脚本路径锚定在文件自身位置，
    不要用 `Path.home()`（root 的 HOME 是 /root）。
- `systemd/` — 两个 unit 模板（`__HOOK_DIR__` / `__NGROK_DOMAIN__`
  由 `ensure.sh` 安装时替换）
- `ensure.sh` — 安装/恢复脚本（幂等；`/etc/systemd/system` 在 VM
  重启后会丢失，重启后重跑一次即可）
- `config.sh.example` — 复制为 `config.sh` 并填你的 ngrok 保留域名

## 部署（见仓库根目录的部署 prompt）

1. 在 [ngrok dashboard](https://dashboard.ngrok.com) 拿 authtoken，
   写到 `/home/hatch/.config/ngrok/ngrok.yml`（0600）；
   再保留一个免费域名填进 `webhook/config.sh`。
2. Muse App 里打开「直接网络协议 → 其他 TCP 连接」，否则 ngrok
   连不上（`ERR_NGROK_9009`）。
3. `sudo bash webhook/ensure.sh`
4. 注册 push subscription：
   `POST https://www.strava.com/api/v3/push_subscriptions`
   参数：`client_id`、`client_secret`、
   `callback_url=https://你的域名/strava-webhook`、
   `verify_token=`（`webhook/receiver_config.json` 里自动生成的）。
   再 `GET /push_subscriptions` 确认。

## 触发延迟

默认 60 秒。可在已有的 `webhook/receiver_config.json` 中增加或修改 `"sync_delay_seconds": 120`，
保留原来的 `verify_token`；旧配置未设置此字段时仍使用 60 秒。新生成的配置会包含默认值。

也可通过接收器进程的环境变量 `WEBHOOK_SYNC_DELAY_SECONDS=120` 覆盖文件配置。
优先级为环境变量 > 配置文件 > 60 秒；取值必须是非负整数秒，`0` 表示不等待。
无效配置会阻止接收器启动并报错。

systemd 部署时，可在服务的 `[Service]` 环境配置中设置：

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

`activity/create` 事件除了触发天气同步，还会运行 `webhook/receiver_config.json`
里配置的个人 hooks——这些是你自己机器上的扩展，不属于本仓库：

```json
{
  "verify_token": "...",
  "sync_delay_seconds": 60,
  "hooks": [
    {"name": "garmin-coros",
     "command": ["/home/hatch/tools/garmin-sync/run_sync.sh", "--account", "account_a", "--headless-only"],
     "cwd": "/home/hatch/tools/garmin-sync"}
  ]
}
```

- `command` 必须是字符串数组（不经过 shell）；`cwd` 可选。
- 每个 hook 以 detached 子进程运行，不阻塞 200 响应；输出记到
  `webhook/logs/hook-<name>-<activity_id>.log`。
- 配置文件是 gitignored 的，按机器存放，仓库保持干净。

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

覆盖 13 种描述场景：旧 gearaut、当前默认、幽灵署名、QWeather、多行山峰与备注（LF / CRLF）、
自定义模板与无域名山峰行、顺序正确、无山峰、关闭排序、无法定位旧天气，以及缺天气时补齐。
另覆盖重复创建事件、后台写入失败后重试、等待后台任务时先回复 HTTP 200，以及更新、删除、
其它对象和非 JSON 事件不触发同步。
同时核对单次详情读取、仅提交变化字段、无变化不写入、手写备注居首，以及延迟配置校验和优先级。
