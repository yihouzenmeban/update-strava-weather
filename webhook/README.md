# Strava webhook 实时推送（替代轮询）

Strava 新活动产生时，官方 webhook 把事件推送到你的公网回调地址，
`receiver.py` 收到后等 60 秒（等 Strava 处理完活动的 GPS 和标题），
再触发 `update_strava_weather.sh sync --activity-id <id>` 拉详情、
查 Open-Meteo 天气、写回活动。每个新活动只花 2 次 Strava API 调用。

## 文件

- `receiver.py` — webhook 接收服务（监听 127.0.0.1:8787）
  - `GET /strava-webhook`：Strava 订阅验证（hub.challenge 握手）
  - `POST /strava-webhook`：activity/create 事件 → 60 秒后触发单活动同步
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
