# Strava webhook 部署与运维

Strava 新活动产生时，官方 webhook 把事件推送到你的公网回调地址，
`receiver.py` 收到后按 `webhook.sync_delay_seconds` 延迟（让 Strava 及其它工具先完成活动处理），
再触发 `update_strava_weather.sh sync --activity-id <id>`。同步行为、模板和离线山峰匹配
统一见 [同步规则](../README.md#同步规则)；本页说明事件、部署、延迟及个人 Hooks。

接收器只触发 `activity/create`，`activity/update` 和 `activity/delete` 均仅回复 200。
缓存跳过和重算边界沿用同步规则；重复事件的 HTTP 200 不代表后台已重新处理活动。

## 文件

- `receiver.py` — webhook 接收服务；监听端点取 `webhook.listen_host` / `listen_port`，回调路径取 `webhook.path`，默认值见 [配置](../config.default.json)。下面以示例回调路径 `/strava-webhook` 表示验证与事件接口：
  - `GET /strava-webhook`：Strava 订阅验证（hub.challenge 握手）
  - `POST /strava-webhook`：activity/create 事件 → 配置的延迟后触发单活动同步
  - `GET /health`：存活探针
- `systemd/` — 两个 unit 模板，路径、域名与监听端点由共享配置生成
- `ensure.sh` — 幂等安装/恢复脚本；写入 systemd unit、启用服务并检查健康端点
- 配置统一使用根目录 `config.default.json` 和 `config.local.json`，加载与旧配置迁移见 [配置说明](../README.md#配置)

## 部署

先按 [首次配置](../README.md#首次配置) 完成 Strava 授权；启用山峰功能时按 [离线库说明](../peaks/README.md) 准备数据库，`ensure.sh` 不下载数据库。本节针对 Linux + systemd + ngrok；仓库中的模板以 root 运行，接收器使用 `/usr/bin/python3`。配置及 `.strava_tokens.json` 须归服务运行用户所有、权限为 `600`，CLI 授权和后台同步应使用同一身份；不同身份的私有文件会被安全检查拒绝。若需非 root 服务，应相应调整 unit 与文件归属，不盲目修改已有凭据。

安装前，在 `config.local.json` 的 `ngrok` 中配置域名、实际可执行文件及 ngrok 自身配置路径；`ensure.sh` 不安装 ngrok，也不创建其授权配置。

1. 安装 ngrok，从 [ngrok dashboard](https://dashboard.ngrok.com) 获取 authtoken，配置到 `ngrok.config_file` 指定的文件，权限设为 `600`。将下列覆盖项合入已有的根目录 `config.local.json`，替换域名及路径：

   ```json
   {
     "ngrok": {
       "domain": "YOUR_NGROK_DOMAIN",
       "executable": "/path/to/ngrok",
       "config_file": "/path/to/ngrok.yml"
     }
   }
   ```

2. 确认服务可直接联网，隧道 unit 会清除代理环境变量。Muse 环境差异见 [MUSE.md](../MUSE.md)。
3. 在仓库根目录运行 `sudo bash webhook/ensure.sh`，检查本地及公网 `/health` 输出。若 unit 丢失，可重跑恢复。修改 ngrok 或监听端点配置后，重跑会重新生成 unit；已有服务还需执行 `sudo systemctl restart strava-webhook-receiver.service strava-webhook-tunnel.service` 才会使用新的启动配置。
4. 注册 push subscription（由部署者使用自己的应用凭据操作）：
   `POST https://www.strava.com/api/v3/push_subscriptions`
   参数：`client_id`、`client_secret`、
   `callback_url=https://你的域名<webhook.path>`（路径含起始 `/`）、
   `verify_token=`（接收器首次启动时自动写入 `config.local.json` 的 `webhook.verify_token`，不会覆盖其它字段；注册时读取该字段，不公开输出）。
   再 `GET /push_subscriptions` 确认。

凭据通过私有配置或标准输入提供，勿写入命令行参数和日志。配置文件及日志不提交仓库。

下面示例在仓库根目录读取共享配置，将注册表单通过标准输入传给 curl，不输出响应中的订阅资料。仅在部署者要求注册订阅且确认配置正确时执行；接收器应已启动并生成验证令牌。

```bash
set +x
set -o pipefail
python3 - <<'PY' | curl -q -fsS --max-time 60 --output /dev/null --write-out 'HTTP %{http_code}\n' --data-binary @- https://www.strava.com/api/v3/push_subscriptions
import sys
from urllib.parse import urlencode
from project_config import load_config

config = load_config()
form = {
    "client_id": config["strava"]["client_id"],
    "client_secret": config["strava"]["client_secret"],
    "callback_url": "https://" + config["ngrok"]["domain"] + config["webhook"]["path"],
    "verify_token": config["webhook"]["verify_token"],
}
if not all((form["client_id"], form["client_secret"], form["verify_token"], config["ngrok"]["domain"])):
    raise SystemExit("缺少订阅配置，请先完成接收器配置")
sys.stdout.write(urlencode(form))
PY
```

订阅查询也须将密钥经标准输入或私有文件传递；勿使用 `curl -G -d client_secret=...` 把密钥放入进程参数。以实际订阅查询及回调验证结果确认注册完成。

## 触发延迟

延迟默认值只在 [config.default.json](../config.default.json) 的 `webhook.sync_delay_seconds` 维护。例如在根目录 `config.local.json` 中合入 `"webhook": {"sync_delay_seconds": 120}`，可设置为 120 秒；保留已有的验证令牌及 Hooks，缺失字段从默认配置继承。

也可通过接收器进程的环境变量 `WEBHOOK_SYNC_DELAY_SECONDS=120` 覆盖文件配置。
优先级为环境变量 > 自定义配置 > 默认配置；取值必须是非负整数秒，`0` 表示不等待。
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

`activity/create` 事件同时启动延迟天气同步和 `config.local.json` 的 `webhook.hooks`。Hooks 立即启动，不等待天气同步，也不受 `sync_delay_seconds` 控制。它们是本机扩展，不属于本仓库。

在已有配置的 `webhook` 对象中增加 `hooks`，保留验证令牌及其它字段。下列路径须替换，勿直接覆盖现有配置：

```json
{
  "webhook": {
    "hooks": [
      {"name": "personal-sync",
       "command": ["/path/to/personal-sync.sh"],
       "cwd": "/path/to/workdir"}
    ]
  }
}
```

- `command` 必须是字符串数组（不经过 shell）；`cwd` 可选。
- 每个 hook 以 detached 子进程运行，不阻塞 200 响应；输出记到
  `webhook/logs/hook-<name>-<activity_id>.log`。
- 配置文件是 gitignored 的，按机器存放，仓库保持干净。

## 排障

先检查服务与本地端点，再检查公网端点（下例的 `127.0.0.1:8787` 是示例监听端点，按实际配置调整并替换域名）：

```bash
systemctl status strava-webhook-receiver.service strava-webhook-tunnel.service
curl -q -fsS --max-time 10 http://127.0.0.1:8787/health
curl -q -fsS --max-time 15 https://YOUR_NGROK_DOMAIN/health
```

- 本地失败：查看 `webhook/logs/receiver.log`，检查 Python 路径、配置和目录访问权限。
- 本地正常、公网失败：查看 `webhook/logs/ngrok.log`，核对 ngrok 路径、授权配置、域名及直接网络访问。
- 已收到创建事件但未更新：延迟结束后查看 `webhook/logs/sync-<activity_id>.log`；根据授权、跳过或失败信息，按 [同步规则](../README.md#同步规则) 排查。收到 HTTP 200 只代表事件已接收，不能据此认定后台同步成功。

## 本地模拟验证

在仓库根目录运行：

```bash
python3 tests/webhook.py
# 或
bash tests/regression.sh webhook
```

测试通过临时本机 HTTP 服务发送真实 POST，请求经过 `receiver.py`、后台子进程和真实的
`sync --activity-id` 流程；Strava / Open-Meteo 使用模拟响应，凭据、活动和处理记录全部位于
临时目录。测试替换 `sleep` 命令跳过实际等待，并核对默认配置、自定义延迟及不等待的参数传递。

覆盖事件筛选、后台响应与失败重试、延迟配置、重复事件跳过，以及不同描述格式下的天气补齐、旧山峰行清理和手写备注保留。山峰命中与新行插入单独由 `python3 tests/peaks.py` 验证；具体场景以测试实现为准。
