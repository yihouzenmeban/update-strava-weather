#!/usr/bin/env bash
# 给 Strava 活动追加 gearaut 风格天气标注
# 一站式脚本：换令牌 / 自动刷新 / 拉取当前活动 / 追加天气标题和描述
#
# 行为：脚本会先拉取活动当前的 name 和 description，
#   - 标题：去掉末尾已有的天气 emoji 后，追加 WEATHER_EMOJI（如 "☁️🥵"）
#   - 描述：去掉旧天气块（兼容旧署名和 "Not Powered By www.gearaut.com 🍚"），追加 WEATHER_DESC
#   即"追加"而非"覆盖"。
#
# 用法：
#   批量补齐新增和待重试活动的天气：./update_strava_weather.sh sync
#   预览：./update_strava_weather.sh sync --dry-run；重算：sync --force
#   历史补扫：sync --since YYYY-MM-DD --until YYYY-MM-DD；全部历史：sync --all
#   1) 首次：复制 strava_config.example.sh 为 strava_config.sh，填好 CLIENT_ID / CLIENT_SECRET；
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
WEATHER_TEMPLATE_FROM_ENV="${WEATHER_TEMPLATE_FILE:-}"
REORDER_SUMMITBAG_FROM_ENV="${REORDER_SUMMITBAG:-}"
# shellcheck source=strava_common.sh
source "$SCRIPT_DIR/strava_common.sh"
export WEATHER_TEMPLATE_FILE="${WEATHER_TEMPLATE_FROM_ENV:-${WEATHER_TEMPLATE_FILE:-}}"
export REORDER_SUMMITBAG="${REORDER_SUMMITBAG_FROM_ENV:-${REORDER_SUMMITBAG:-1}}"

# 要追加的天气 emoji 和天气描述块（可被环境变量覆盖，或用 *_FILE 指定文件路径，文件优先）
WEATHER_EMOJI="${WEATHER_EMOJI:-}"
WEATHER_DESC="${WEATHER_DESC:-}"
[[ -n "${WEATHER_EMOJI_FILE:-}" && -f "$WEATHER_EMOJI_FILE" ]] && WEATHER_EMOJI=$(cat "$WEATHER_EMOJI_FILE")
[[ -n "${WEATHER_DESC_FILE:-}" && -f "$WEATHER_DESC_FILE" ]] && WEATHER_DESC=$(cat "$WEATHER_DESC_FILE")

check_config() {
  if [[ -z "${CLIENT_ID:-}" || "$CLIENT_ID" == *"在这里粘贴"* ]]; then
    die "请先创建 strava_config.sh（参考 strava_config.example.sh）并填好 CLIENT_ID"
  fi
  if [[ -z "${CLIENT_SECRET:-}" || "$CLIENT_SECRET" == *"在这里粘贴"* ]]; then
    die "请先创建 strava_config.sh（参考 strava_config.example.sh）并填好 CLIENT_SECRET"
  fi
  return 0
}

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

# 确保拿到未过期的 access_token：有效就直接用，过期就刷新，都不行就返回非零交由调用方走 login
ensure_token() {
  if [[ ! -f "$TOKEN_FILE" ]]; then
    echo "→ 没有令牌缓存，需要授权"
    return 1
  fi
  local access refresh expires now
  access=$(token_field access_token)
  refresh=$(token_field refresh_token)
  expires=$(token_field expires_at)
  if [[ -z "$access" || -z "$refresh" ]]; then
    echo "→ 令牌缓存不完整，需要重新授权"
    return 1
  fi

  now=$(date +%s)
  if (( now < expires - 60 )); then
    echo "→ 令牌仍有效，跳过刷新"
    return 0
  fi

  echo "→ 令牌已过期，刷新中..."
  local resp
  resp=$(oauth_token_request refresh_token "$refresh")
  if ! echo "$resp" | python3 -c '
import json,sys
d=json.load(sys.stdin)
if "access_token" not in d:
    print("→ 刷新失败（refresh_token 可能已失效），转为重新授权");sys.exit(1)
print("✅ 已刷新")
'; then
    return 1
  fi
  NEW=$(echo "$resp" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(json.dumps({k:d[k] for k in ("access_token","refresh_token","expires_at","scope") if k in d}))')
  save_tokens
  return 0
}

require_token() {
  if ensure_token; then
    return 0
  fi
  [[ "${STRAVA_NONINTERACTIVE:-}" != "1" ]] || die "请先运行 ./update_strava_weather.sh login 完成授权，再重试 sync"
  do_login
}

# 只读 API 用子 shell 管理临时文件，不影响更新命令的 EXIT trap。
do_read() (
  local endpoint
  case "$1" in
    list)
      local count="${2:-1}"
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

  require_token >&2
  local access response_file code
  access=$(token_field access_token)
  response_file=$(mktemp "${TMPDIR:-/tmp}/strava-read.XXXXXX")
  trap 'rm -f "$response_file"' EXIT
  if ! code=$(strava_curl "$access" \
    -o "$response_file" -w "%{http_code}" \
    "https://www.strava.com/api/v3/${endpoint}"); then
    die "Strava API 网络请求失败"
  fi
  check_api_status "$code"
  python3 - "$response_file" <<'PY'
import json, sys

try:
    with open(sys.argv[1], encoding="utf-8") as response:
        data = json.load(response)
except (ValueError, OSError):
    print("❌ Strava API 返回了无效的 JSON 响应", file=sys.stderr)
    sys.exit(1)
json.dump(data, sys.stdout, ensure_ascii=False)
print()
PY
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

  # 1) 拉取活动当前内容
  echo "→ 拉取活动 $ACTIVITY_ID 当前内容..."
  code=$(strava_curl "$access" -o "$work_dir/activity.json" -w "%{http_code}" \
    "https://www.strava.com/api/v3/activities/${ACTIVITY_ID}") || die "Strava API 网络请求失败"
  check_api_status "$code"

  # 2) 计算追加后的 name / description，写到 body 文件
  WEATHER_EMOJI="$WEATHER_EMOJI" WEATHER_DESC="$WEATHER_DESC" \
    python3 - "$work_dir/activity.json" "$body_file" "$SCRIPT_DIR" <<'PY'
import json, sys, os, re

sys.path.insert(0, sys.argv[3])
from weather_template import has_weather, load_template, reorder_summitbag, strip_weather

try:
    template = load_template(os.environ.get("WEATHER_TEMPLATE_FILE"))
except ValueError as error:
    print(f"❌ {error}", file=sys.stderr)
    sys.exit(1)

act = json.load(open(sys.argv[1], encoding="utf-8"))
if "id" not in act:
    print("❌ 拉取活动失败:", json.dumps(act, ensure_ascii=False)[:300]); sys.exit(1)

# 跑步机等室内活动没有真实户外天气，跳过天气标注
# 判定：trainer=true（ treadmill ）或 sport_type 为 Virtual* 系列
# FORCE=1 可强制处理（用于清理之前误标的跑步机活动）
sport_type = act.get("sport_type", "") or ""
is_trainer = bool(act.get("trainer", False))
is_virtual = sport_type.startswith("Virtual")
if (is_trainer or is_virtual) and os.environ.get("FORCE", "") != "1":
    print(f"⏭️ 跳过跑步机/室内活动（sport_type={sport_type}, trainer={is_trainer}），不更新天气")
    print("   如需清理已误标的旧天气标注，请加 FORCE=1")
    sys.exit(2)

name = act.get("name", "") or ""
desc = act.get("description", "") or ""
reorder_enabled = os.environ.get("REORDER_SUMMITBAG") == "1"
reorder_only = os.environ.get("STRAVA_REORDER_ONLY") == "1" or (
    os.environ.get("STRAVA_SYNC_CHECK_WEATHER") == "1" and has_weather(desc, template))
if reorder_only:
    new_name = name
    new_desc = reorder_summitbag(desc, template, reorder_enabled)
    if new_desc == desc:
        print("⏭️ 保留最新天气，山峰无需排序，不重复写入")
        sys.exit(3)
else:
    emoji = os.environ["WEATHER_EMOJI"].strip()
    block = os.environ["WEATHER_DESC"].strip()

    # 标题：去掉末尾已有的天气 emoji（含 variation selector、空格），再追加新 emoji
    # 覆盖：符号区 U+2600-U+27BF、象形区 U+1F300-U+1F5FF、补充区 U+1F900-U+1F9FF（含 🥵🥶）
    emoji_tail = re.compile(r'[\s️‍☀-➿\U0001F300-\U0001F5FF\U0001F900-\U0001F9FF]+$')
    name_base = emoji_tail.sub('', name)
    new_name = (name_base + " " + emoji).strip() if emoji else name_base

    # 描述：去掉旧天气块（兼容历史署名），再追加新块
    try:
        desc_clean = strip_weather(desc, template)
    except ValueError as error:
        print(f"❌ {error}", file=sys.stderr)
        sys.exit(1)
    if block:
        new_desc = (desc_clean + "\n" + block) if desc_clean else block
    else:
        new_desc = desc_clean
    new_desc = reorder_summitbag(new_desc, template, reorder_enabled)

json.dump({"name": new_name, "description": new_desc},
          open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
print("  原标题:", name)
print("  新标题:", new_name)
if desc:
    print("  原描述:", desc.replace("\n", " | "))
print("  新描述:")
print(new_desc)
PY

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
  *) die "未知命令: $1（可用: sync / login / auth / list [数量] / get / streams / show / 不带参数直接运行）" ;;
esac
