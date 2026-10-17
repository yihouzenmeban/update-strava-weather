# Strava webhook 实时推送（替代轮询）

Strava 新活动产生时，官方 webhook 把事件推送到你的公网回调地址，
`receiver.py` 收到后等 60 秒（等 Strava 处理完活动的 GPS 和标题），
再触发 `update_strava_weather.sh sync --activity-id <id>` 拉详情、
需要时查 Open-Meteo 天气、写回活动。起点坐标完整时，新补天气或仅调整山峰排序
使用 3 次 Strava API 调用（读详情、写入前再读、更新）；默认开启排序且无需修改时只读 1 次详情。
已处理活动也会按默认配置检查后来追加的山峰数据；可设置 `REORDER_SUMMITBAG=0` 关闭。

接收器只触发 `activity/create`，`activity/update` 和 `activity/delete` 均仅回复 200。
创建事件处理完成后才追加的山峰数据，依靠定时 `sync` 再次扫描，或手动指定活动 ID 同步。

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

## 本地模拟验证

在仓库根目录运行：

```bash
python3 tests/webhook.py
# 或
bash tests/regression.sh webhook
```

测试通过临时本机 HTTP 服务发送真实 POST，请求经过 `receiver.py`、后台子进程和真实的
`sync --activity-id` 流程；Strava / Open-Meteo 使用模拟响应，凭据、活动和处理记录全部位于
临时目录。仅测试替换 `sleep` 命令跳过 60 秒等待，生产触发命令和延迟保持原样。

覆盖 13 种描述场景：旧 gearaut、当前默认、幽灵署名、QWeather、多行山峰与备注（LF / CRLF）、
自定义模板与无域名山峰行、顺序正确、无山峰、关闭排序、无法定位旧天气，以及缺天气时补齐。
另覆盖重复创建事件、后台写入失败后重试、等待后台任务时先回复 HTTP 200，以及更新、删除、
其它对象和非 JSON 事件不触发同步。
