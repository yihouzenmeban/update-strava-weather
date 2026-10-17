# 仓库贡献指南

## 项目结构与模块职责

- `update_strava_weather.sh` 是命令入口，`strava_common.sh` 负责凭据与 HTTP 请求；`strava_oauth.py` 处理授权，`strava_sync.py` 负责活动发现、重试、并发锁和天气计算。
- `weather_template.py` 处理描述渲染与山峰排序，默认模板位于 `templates/weather.default.txt`；`backfill_all.sh`、`reorder_summitbag.sh` 提供维护命令。
- `webhook/` 包含接收器、部署脚本和 systemd 模板；`tests/` 存放隔离回归测试。个人集成通过各机器的 webhook hooks 配置接入。

## Agent 入口与技能维护

`AGENTS.md` 是共享规则源，`CLAUDE.md` 通过相对软链引用它。技能实际存放在 `.agents/skills/`，`.claude/skills` 软链到该目录。维护时修改真实文件并保留相对软链，使不同 Agent 共用同一份内容。

## 开发与验证命令

运行环境需要 Bash、Python 3.9+ 和 curl，无需构建。

```bash
bash tests/regression.sh                 # 完整离线回归
python3 tests/sync.py                    # 同步流程测试
python3 tests/webhook.py                 # 本机 HTTP webhook 集成测试
python3 tests/templates.py               # 模板渲染与描述排序
bash -n update_strava_weather.sh strava_common.sh
git diff --check
```

真实环境开发时，参考示例创建 `strava_config.sh`，再运行 `./update_strava_weather.sh login`。`./update_strava_weather.sh sync --dry-run` 会读取真实 API，但不更新活动或同步状态。

## 代码风格与命名

Python 使用四空格缩进，Bash 使用两空格缩进；函数使用 `snake_case`，常量与环境变量使用大写。优先使用 Python 标准库，显式处理错误，为 Shell 变量加引号，保持小范围修改并沿用周边风格。当前未配置格式化或静态检查工具，使用 Bash 语法检查与差异检查。

## 测试要求

测试使用 `unittest`、模拟 API、临时目录及本机 HTTP 服务，测试方法命名为 `test_<行为>`。行为变更应先补测试并确认预期失败，再实现、复跑相关测试及完整回归；当前没有数值覆盖率门槛。

覆盖请求次数、失败重试、预览、幂等性及手写备注保留。每次处理只读取一次活动详情，只提交变化字段；天气前的手写备注保持在山峰与天气之前。不添加仅验证已删除代码仍不存在的测试。

## 提交与 PR 约定

编辑前先检查 `git status`，保留无关或并发修改，只暂存本次任务文件。提交沿用 `feat:`、`test:`、`docs:` 等前缀，说明使用中文；完成文件变更后在当前分支提交。仅在用户明确要求时推送或改写历史。

PR 说明应描述行为、配置影响、相关问题及验证命令和结果；描述格式变更应附脱敏的前后对照。沟通默认使用中文，步骤与命令保持简洁明确。

## 安全与配置

不提交凭据、令牌、处理记录、日志或个人 hooks 配置。敏感文件权限保持 `600`，临时目录保持 `700`。使用配置示例，机器专属集成保留在本地配置中，不写入共享应用代码。
