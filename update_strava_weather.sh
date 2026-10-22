#!/usr/bin/env bash
# 给 Strava 活动追加 gearaut 风格天气标注
# 一站式脚本：换令牌 / 自动刷新 / 拉取当前活动 / 追加天气标题和描述
#
# 行为：脚本会先拉取活动当前的 name 和 description，
#   - 标题：按 title.template 渲染，默认去掉末尾旧天气 emoji 后追加 WEATHER_EMOJI
#   - 描述：去掉旧天气块（兼容旧署名和 "Not Powered By www.gearaut.com 🍚"），追加 WEATHER_DESC
#   即"追加"而非"覆盖"。
#
# 用法：
#   批量补齐新增和待重试活动的天气：./update_strava_weather.sh sync
#   预览：./update_strava_weather.sh sync --dry-run；重算：sync --force
#   历史补扫：sync --since YYYY-MM-DD --until YYYY-MM-DD；全部历史：sync --all
#   查看 API 用量与预留额度：./update_strava_weather.sh api-usage
#   1) 首次：在 config.local.json 的 strava 中填写 client_id / client_secret；
#      确认 Strava 应用 Authorization Callback Domain = localhost；然后运行：
#        ./update_strava_weather.sh login
#   2) 预览将要追加的标题/描述（不改 Strava）：
#        ACTIVITY_ID=xxx WEATHER_EMOJI='☁️🥵' WEATHER_DESC_FILE=/path/desc.txt ./update_strava_weather.sh show
#   3) 更新（会先打印新旧对比，再问 [y/N] 确认）：
#        ACTIVITY_ID=xxx WEATHER_EMOJI='☁️🥵' WEATHER_DESC_FILE=/path/desc.txt ./update_strava_weather.sh
#   环境变量：
#     ACTIVITY_ID            手动更新/get/streams 必填；sync 用 --activity-id 指定活动
#     WEATHER_EMOJI          追加到标题的 emoji（可空，或用 WEATHER_EMOJI_FILE 指定文件）
#     WEATHER_DESC           追加到描述的天气块（可空，或用 WEATHER_DESC_FILE 指定文件）
#     REORDER_SUMMITBAG=0     关闭山峰数据排到天气前面（默认开启）
#     DRY_RUN=1              仅预览不更新（等同 show 子命令）
#     YES=1                  跳过确认直接更新（自动化/skill 用）
#
# 也可以手动拿 code 后运行 ./update_strava_weather.sh auth，从标准输入提供授权码。
# 只读 API（stdout 为 JSON，授权/刷新提示写 stderr）：
#   ./update_strava_weather.sh list [数量]（默认 1）
#     可用 STRAVA_PAGE / STRAVA_AFTER / STRAVA_BEFORE 指定页码和 Unix 时间戳范围
#   ACTIVITY_ID=xxx ./update_strava_weather.sh get
#   ACTIVITY_ID=xxx ./update_strava_weather.sh streams（坐标流）

set +x
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=strava_common.sh
source "$SCRIPT_DIR/strava_common.sh"

# 要追加的天气 emoji 和天气描述块（可被环境变量覆盖，或用 *_FILE 指定文件路径，文件优先）
WEATHER_EMOJI="${WEATHER_EMOJI:-}"
WEATHER_DESC="${WEATHER_DESC:-}"
[[ -n "${WEATHER_EMOJI_FILE:-}" && -f "$WEATHER_EMOJI_FILE" ]] && WEATHER_EMOJI=$(cat "$WEATHER_EMOJI_FILE")
[[ -n "${WEATHER_DESC_FILE:-}" && -f "$WEATHER_DESC_FILE" ]] && WEATHER_DESC=$(cat "$WEATHER_DESC_FILE")

# 启动本地服务接住授权回调，自动拿 code 并换令牌（无需自己起服务）
do_login() {
  local code
  code=$(python3 "$SCRIPT_DIR/strava_oauth.py" "$CLIENT_ID") || die "Strava 授权未完成"
  [[ -n "$code" ]] || die "未收到授权码"
  do_auth "$code"
}

# 用授权码换令牌
do_auth() {
  local code="$1"
  [[ -z "$code" ]] && die "用法: $0 auth（从标准输入读取授权码）"
  echo "→ 用授权码换取令牌..."
  local resp
  resp=$(oauth_token_request authorization_code "$code")
  echo "$resp" | python3 -c '
import json,sys,time
d=json.load(sys.stdin)
if "access_token" not in d:
    print("❌ 换取失败，请检查授权码及应用配置", file=sys.stderr);sys.exit(1)
print("✅ 换取成功")
print("  athlete id:",d.get("athlete",{}).get("id"))
print("  scope     :",d.get("scope",""))
print("  过期时间  :",time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(d["expires_at"])))
'
  # 把返回写进缓存
  NEW=$(echo "$resp" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(json.dumps({k:d[k] for k in ("access_token","refresh_token","expires_at","scope") if k in d}))')
  save_tokens
  echo "→ 令牌已保存到 $TOKEN_FILE"
}

# 只读 API 用子 shell 管理临时文件，不影响更新命令的 EXIT trap。
do_read() (
  local endpoint
  case "$1" in
    list)
      local count="${2:-$STRAVA_LIST_COUNT}"
      [[ "$count" =~ ^[1-9][0-9]{0,2}$ ]] && (( count <= 200 )) || die "活动数量必须为 1–200 的整数"
      local page="${STRAVA_PAGE:-1}"
      [[ "$page" =~ ^[1-9][0-9]{0,8}$ ]] || die "STRAVA_PAGE 必须为正整数"
      endpoint="athlete/activities?page=${page}&per_page=${count}"
      if [[ -n "${STRAVA_AFTER:-}" ]]; then
        [[ "$STRAVA_AFTER" =~ ^[0-9]{1,12}$ ]] || die "STRAVA_AFTER 必须为非负整数时间戳"
        endpoint+="&after=${STRAVA_AFTER}"
      fi
      if [[ -n "${STRAVA_BEFORE:-}" ]]; then
        [[ "$STRAVA_BEFORE" =~ ^[0-9]{1,12}$ ]] || die "STRAVA_BEFORE 必须为非负整数时间戳"
        endpoint+="&before=${STRAVA_BEFORE}"
      fi
      ;;
    get|streams)
      [[ "${ACTIVITY_ID:-}" =~ ^[1-9][0-9]*$ ]] || die "请通过环境变量 ACTIVITY_ID 指定有效的活动 ID（正整数）"
      endpoint="activities/${ACTIVITY_ID}"
      [[ "$1" != "streams" ]] || endpoint+="/streams?keys=latlng&key_by_type=true"
      ;;
  esac

  strava_request GET "https://www.strava.com/api/v3/${endpoint}"
)

# 更新活动：拉取当前 name/description，去掉旧天气标注，追加新天气后 PUT
do_update() (
  [[ "${ACTIVITY_ID:-}" =~ ^[1-9][0-9]*$ ]] || die "请通过环境变量 ACTIVITY_ID 指定有效的活动 ID（正整数）"
  [[ "$REORDER_SUMMITBAG" == "0" || "$REORDER_SUMMITBAG" == "1" ]] || die "REORDER_SUMMITBAG 必须为 0 或 1"

  require_token
  local access work_dir body_file code
  access=$(token_field access_token)
  work_dir=$(mktemp -d "${TMPDIR:-/tmp}/strava-weather.XXXXXX")
  trap 'rm -rf "$work_dir"' EXIT
  body_file="$work_dir/body.json"

  # sync 已读取详情；手动更新时在这里读取一次。
  if [[ -n "${STRAVA_SYNC_ACTIVITY_FILE:-}" ]]; then
    cp "$STRAVA_SYNC_ACTIVITY_FILE" "$work_dir/activity.json" || die "无法读取活动快照"
  else
    echo "→ 拉取活动 $ACTIVITY_ID 当前内容..."
    code=$(strava_curl "$access" -o "$work_dir/activity.json" -w "%{http_code}" \
      "https://www.strava.com/api/v3/activities/${ACTIVITY_ID}") || die "Strava API 网络请求失败"
    check_api_status "$code"
  fi

  # 2) 计算追加后的 name / description，写到 body 文件
  WEATHER_EMOJI="$WEATHER_EMOJI" WEATHER_DESC="$WEATHER_DESC" \
    python3 "$SCRIPT_DIR/activity_update.py" "$work_dir/activity.json" "$body_file" "$ACTIVITY_ID"

  [[ -f "$body_file" ]] || return 0

  # 3) 确认后再覆盖（DRY_RUN=1 仅预览不更新；YES=1 跳过确认）
  if [[ "${DRY_RUN:-}" == "1" ]]; then
    echo "→ DRY_RUN=1，仅预览不更新"
    return 0
  fi
  if [[ "${YES:-}" != "1" ]]; then
    echo
    read -r -p "→ 确认覆盖此活动？[y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || die "已取消，未更新"
  fi

  # 4) PUT 更新
  echo "→ 正在更新活动 $ACTIVITY_ID ..."
  code=$(strava_curl "$access" -o "$work_dir/response.json" -w "%{http_code}" \
    -X PUT "https://www.strava.com/api/v3/activities/${ACTIVITY_ID}" \
    -H "Content-Type: application/json; charset=utf-8" \
    --data-binary "@${body_file}") || die "Strava API 网络请求失败"
  check_api_status "$code"

  echo "→ HTTP 状态码: $code"
  python3 - "$work_dir/response.json" "$ACTIVITY_ID" <<'PY'
import json, sys
d=json.load(open(sys.argv[1]))
if str(d.get("id")) == sys.argv[2]:
    print("✅ 更新成功")
    print("  name       :",d.get("name"))
    print("  description:",d.get("description"))
else:
    print("❌ 失败:",json.dumps(d,ensure_ascii=False,indent=2))
    sys.exit(1)
PY
)

# ====== 入口 ======
check_config
case "${1:-run}" in
  sync) shift; python3 "$SCRIPT_DIR/strava_sync.py" "$@" ;;
  login) do_login ;;
  auth)
    [[ $# -eq 1 ]] || die "用法: $0 auth（从标准输入读取授权码，不接受命令行参数）"
    code=""
    read -r -s -p "→ 粘贴授权码: " code || [[ -n "$code" ]] || die "用法: $0 auth（从标准输入读取授权码）"
    echo >&2
    do_auth "$code"
    ;;
  list|get|streams) do_read "$1" "${2:-}" ;;
  show) DRY_RUN=1 do_update ;;
  run|"") do_update ;;
  api-usage) python3 "$SCRIPT_DIR/api_usage.py" status ;;
esac
