# 复制本文件为 strava_config.sh，填入你自己的 Strava 应用密钥
# strava_config.sh 已被 .gitignore 忽略，不会提交
#
# 获取方式：到 https://www.strava.com/settings/api 注册应用，拿到 Client ID 和 Client Secret
# 同时把应用的 Authorization Callback Domain 设为 localhost

CLIENT_ID="在这里粘贴你的Client ID"
CLIENT_SECRET="在这里粘贴你的Client Secret"

# 可选：自定义天气描述模板（UTF-8 文本，相对路径以仓库根目录为准）
# 优先级：sync --template > 环境变量 WEATHER_TEMPLATE_FILE > 此处配置 > 默认模板
# WEATHER_TEMPLATE_FILE="weather.custom.txt"
