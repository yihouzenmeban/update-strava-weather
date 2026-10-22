# 在 Muse 上部署 Strava 天气同步

本页补充 Muse 环境的部署差异。只在用户要求部署或维护服务时执行；普通代码任务不安装服务或创建平台定时任务。

通用说明各维护一份：

- [README](README.md#首次配置)：运行依赖、私有配置、OAuth、同步命令与模板。
- [离线山峰库](peaks/README.md)：新机器下载、表结构检查和匹配规则。
- [webhook 部署](webhook/README.md#部署)：ngrok、systemd、订阅注册、延迟、Hooks 与排障。
- [API 用量与配额](README.md#api-用量与配额)：本地计数、批量暂停和补扫边界。

## 部署准备

1. 让用户通过安全渠道提供 Strava 应用凭据和 ngrok 授权；应用 Callback Domain 使用 `localhost`，OAuth 操作按 README 完成，不在聊天中复述凭据。
2. 在 Muse 设置中检查「直接网络协议 → 其他 TCP 连接」。此前部署遇到过未开启时 ngrok 报 `ERR_NGROK_9009`；界面及平台行为可能变化，应以当前环境验证为准。仓库隧道 unit 已清除代理环境变量。
3. 克隆仓库，确认实际工作目录，不假定文件位于某个固定家目录：

   ```bash
   git clone https://github.com/yihouzenmeban/update-strava-weather.git
   cd update-strava-weather
   ```

4. 按通用文档合入 `config.local.json`，配置实际的 `ngrok.executable`、`ngrok.config_file` 和域名；不要把当前 shell 的 `~` 当作服务用户的家目录。
5. 准备离线库后运行预览，再按 webhook 文档安装、注册订阅。服务模板以 root 运行，配置及令牌必须与服务身份一致，具体权限要求见通用部署说明。

不要在 prompt、命令行参数或日志中写入应用密钥、验证令牌、OAuth token 或网页登录 cookie。

## 验证部署

检查本地及公网 `/health`，然后查看真实创建事件对应的 `webhook/logs/sync-<activity_id>.log`；HTTP 200 只代表事件已接收。等待时间按配置，不承诺固定完成时刻。

按 [同步规则](README.md#同步规则) 核对日志中的标题、描述、跳过原因、山峰匹配警告和待处理数量；需要重算时先确认修改范围。

端到端测试优先使用用户已有且允许更新的活动。另建测试活动及删除操作须符合用户授权，不自动创建或删除真实活动。

## 可选的平台恢复任务

此前 Muse 部署出现过 VM 替换后 `/etc/systemd/system` 丢失的情况。先检查当前环境是否仍存在此问题；需要恢复机制且用户已要求定时维护时，再使用平台支持的持久调度方式，不假定普通 VM 内 cron 会跨替换保留。

每轮维护的步骤：

1. 检查 `strava-webhook-receiver.service` 和 `strava-webhook-tunnel.service`。
2. unit 丢失时，在实际仓库目录运行 `sudo bash webhook/ensure.sh`；已有 unit 配置变化时按 webhook 文档重新生成并重启。
3. 恢复后可运行一次 `./update_strava_weather.sh sync` 补漏，不连续重试。
4. 检查 `⏸️`、失败与待处理信息；退出码 0 不保证全部完成。日常成功静默，恢复或失败时报告。

由部署者按可接受停机时间设置周期。本仓库不提供或自动安装平台调度任务。

## 安全升级

在部署仓库检查分支和工作区；仅当处于跟踪 `origin/main` 的 `main` 且无本地修改时执行以下快进升级。其它情况先报告并保留现有修改，不自动覆盖：

```bash
git status --short
git branch --show-current
git fetch origin
git merge --ff-only origin/main
python3 project_config.py check
```

不要使用 `git reset --hard` 升级。配置和数据库留在部署仓库的私有位置，不复制或输出敏感文件。数据库升级由部署者按离线库说明单独管理。

接收器由当前仓库路径启动，不需要另复制 `receiver.py`。接收器代码或配置变化后重启；unit 模板、ngrok 或监听端点变化后，先重跑 `sudo bash webhook/ensure.sh` 生成 unit，再按通用部署文档重启服务并检查健康端点。无需通过批量 `chmod +x` 修改仓库文件。

历史补扫使用 `bash backfill_all.sh`；配额预留暂停与实际 HTTP 429 的退避不同，运行前先读 README 的配额说明。个人 Hooks 只在本机配置，维护方法见 webhook 文档。
