# 复制本文件为 strava_config.sh，填入你自己的 Strava 应用密钥
# strava_config.sh 已被 .gitignore 忽略，不会提交
#
# 获取方式：到 https://www.strava.com/settings/api 注册应用，拿到 Client ID 和 Client Secret
# 同时把应用的 Authorization Callback Domain 设为 localhost

CLIENT_ID="在这里粘贴你的Client ID"
CLIENT_SECRET="在这里粘贴你的Client Secret"

# 天气数据源：open-meteo（默认，无需密钥）或 qweather（和风天气）
# 支持环境变量覆盖；也可通过 sync --weather-provider 临时选择
WEATHER_PROVIDER="${WEATHER_PROVIDER:-open-meteo}"

# 选择 qweather 时配置；请通过环境变量提供真实凭据，勿写入仓库
# API Host 从和风控制台获取，只填域名，如 abc123.xy.qweatherapi.com
QWEATHER_API_HOST="${QWEATHER_API_HOST:-}"
QWEATHER_API_KEY="${QWEATHER_API_KEY:-}"
# 可改用自行生成的有效 JWT；设置后优先使用 JWT，不同时发送 API Key
QWEATHER_JWT="${QWEATHER_JWT:-}"
