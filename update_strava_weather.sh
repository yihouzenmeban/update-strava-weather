#!/usr/bin/env bash
# 给 Strava 活动追加 gearaut 风格天气标注
# 一站式脚本：换令牌 / 自动刷新 / 拉取当前活动 / 追加天气标题和描述
#
# 行为：脚本会先拉取活动当前的 name 和 description，
#   - 标题：去掉末尾已有的天气 emoji 后，追加 WEATHER_EMOJI（如 "☁️🥵"）
#   - 描述：去掉旧的 "温度:...Powered By www.gearaut.com" 块后，追加 WEATHER_DESC
#   即"追加"而非"覆盖"。
#
# 用法：
#   1) 首次：复制 strava_config.example.sh 为 strava_config.sh，填好 CLIENT_ID / CLIENT_SECRET；
#      确认 Strava 应用 Authorization Callback Domain = localhost；然后运行：
#        ./update_strava_weather.sh login
#   2) 预览将要追加的标题/描述（不改 Strava）：
#        ACTIVITY_ID=xxx WEATHER_EMOJI='☁️🥵' WEATHER_DESC_FILE=/path/desc.txt ./update_strava_weather.sh show
#   3) 更新（会先打印新旧对比，再问 [y/N] 确认）：
#        ACTIVITY_ID=xxx WEATHER_EMOJI='☁️🥵' WEATHER_DESC_FILE=/path/desc.txt ./update_strava_weather.sh
#   环境变量：
#     ACTIVITY_ID            必填，要更新的活动 ID
#     WEATHER_EMOJI          追加到标题的 emoji（可空，或用 WEATHER_EMOJI_FILE 指定文件）
#     WEATHER_DESC           追加到描述的天气块（可空，或用 WEATHER_DESC_FILE 指定文件）
#     DRY_RUN=1              仅预览不更新（等同 show 子命令）
#     YES=1                  跳过确认直接更新（自动化/skill 用）
#
# 也可以手动拿 code 后用：./update_strava_weather.sh auth <code>
# 只读 API（stdout 为 JSON，授权/刷新提示写 stderr）：
#   ./update_strava_weather.sh list [数量]（默认 1）
#   ACTIVITY_ID=xxx ./update_strava_weather.sh get
#   ACTIVITY_ID=xxx ./update_strava_weather.sh streams（坐标流）

set -euo pipefail

# ====== 配置区 ======
# 从同目录 strava_config.sh 读取 CLIENT_ID / CLIENT_SECRET（该文件含密钥，已 gitignore）
CONFIG_FILE="$(dirname "$0")/strava_config.sh"
if [[ -f "$CONFIG_FILE" ]]; then
  # shellcheck source=strava_config.sh
  source "$CONFIG_FILE"
fi
TOKEN_FILE="$(dirname "$0")/.strava_tokens.json"
# ====================

# 要追加的天气 emoji 和天气描述块（可被环境变量覆盖，或用 *_FILE 指定文件路径，文件优先）
WEATHER_EMOJI="${WEATHER_EMOJI:-}"
WEATHER_DESC="${WEATHER_DESC:-}"
[[ -n "${WEATHER_EMOJI_FILE:-}" && -f "$WEATHER_EMOJI_FILE" ]] && WEATHER_EMOJI=$(cat "$WEATHER_EMOJI_FILE")
[[ -n "${WEATHER_DESC_FILE:-}" && -f "$WEATHER_DESC_FILE" ]] && WEATHER_DESC=$(cat "$WEATHER_DESC_FILE")

die() { echo "❌ $*" >&2; exit 1; }

check_config() {
  if [[ -z "${CLIENT_ID:-}" || "$CLIENT_ID" == *"在这里粘贴"* ]]; then
    die "请先创建 strava_config.sh（参考 strava_config.example.sh）并填好 CLIENT_ID"
  fi
  if [[ -z "${CLIENT_SECRET:-}" || "$CLIENT_SECRET" == *"在这里粘贴"* ]]; then
    die "请先创建 strava_config.sh（参考 strava_config.example.sh）并填好 CLIENT_SECRET"
  fi
  return 0
}

# 读取缓存里的某个字段
token_field() { python3 -c "import json,sys;print(json.load(open('$TOKEN_FILE')).get('$1',''))"; }

# 保存令牌到缓存文件（通过 NEW 环境变量传入新令牌 JSON）
save_tokens() {
  NEW="$NEW" python3 - "$TOKEN_FILE" <<'PY'
import json, sys, os
new = json.loads(os.environ["NEW"])
path = sys.argv[1]
d = json.load(open(path)) if os.path.exists(path) else {}
d.update({k: new[k] for k in ("access_token","refresh_token","expires_at","scope") if k in new})
json.dump(d, open(path, "w"), ensure_ascii=False, indent=2)
PY
}

# 启动本地服务接住授权回调，自动拿 code 并换令牌（无需自己起服务）
do_login() {
  # 先让系统分配一个可用端口，避免端口被占用
  local port
  port=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
  echo "→ 启动本地服务 http://localhost:${port} 等待授权回调..."
  local url="https://www.strava.com/oauth/authorize?client_id=${CLIENT_ID}&redirect_uri=http://localhost:${port}&response_type=code&approval_prompt=force&scope=activity:read_all,activity:write"
  echo "→ 正在自动打开浏览器进行授权（若没打开，手动复制下面 URL）："
  echo "$url"
  # 自动打开浏览器（macOS / Linux / Windows）
  if command -v open >/dev/null 2>&1; then open "$url"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$url"
  elif command -v start >/dev/null 2>&1; then start "$url"; fi
  echo
  echo "→ 授权后浏览器会跳转到本地服务，本脚本自动捕获 code 并换取令牌..."
  local code
  code=$(python3 - "$port" <<'PY'
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
port=int(sys.argv[1])
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        q=parse_qs(urlparse(self.path).query)
        code=q.get('code',[None])[0]
        self.send_response(200)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.end_headers()
        if code:
            self.wfile.write("✅ 授权成功，可以关闭此页面并回到终端".encode())
            print(code, flush=True)
        else:
            self.wfile.write("未收到 code".encode())
    def log_message(self,*a): pass
HTTPServer(('127.0.0.1',port), H).handle_request()
PY
)
  [[ -z "$code" ]] && die "未收到授权码"
  do_auth "$code"
}

# 用授权码换令牌
do_auth() {
  local code="$1"
  [[ -z "$code" ]] && die "用法: $0 auth <code>"
  echo "→ 用授权码换取令牌..."
  local resp
  resp=$(curl -s -X POST "https://www.strava.com/api/v3/oauth/token" \
    -d "client_id=${CLIENT_ID}" \
    -d "client_secret=${CLIENT_SECRET}" \
    -d "code=${code}" \
    -d "grant_type=authorization_code")
  echo "$resp" | python3 -c '
import json,sys,time
d=json.load(sys.stdin)
if "access_token" not in d:
    print("❌ 换取失败:",json.dumps(d,ensure_ascii=False,indent=2));sys.exit(1)
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
  resp=$(curl -s -X POST "https://www.strava.com/api/v3/oauth/token" \
    -d "client_id=${CLIENT_ID}" \
    -d "client_secret=${CLIENT_SECRET}" \
    -d "refresh_token=${refresh}" \
    -d "grant_type=refresh_token")
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

# 只读 API 用子 shell 管理临时文件，不影响更新命令的 EXIT trap。
do_read() (
  local endpoint
  case "$1" in
    list)
      local count="${2:-1}"
      [[ "$count" =~ ^[1-9][0-9]{0,2}$ ]] && (( count <= 200 )) || die "活动数量必须为 1–200 的整数"
      endpoint="athlete/activities?page=1&per_page=${count}"
      ;;
    get|streams)
      [[ "${ACTIVITY_ID:-}" =~ ^[1-9][0-9]*$ ]] || die "请通过环境变量 ACTIVITY_ID 指定有效的活动 ID（正整数）"
      endpoint="activities/${ACTIVITY_ID}"
      [[ "$1" != "streams" ]] || endpoint+="/streams?keys=latlng&key_by_type=true"
      ;;
  esac

  if ! ensure_token >&2; then
    do_login >&2
  fi
  local access response_file code
  access=$(token_field access_token)
  response_file=$(mktemp -t strava_read)
  trap 'rm -f "$response_file"' EXIT
  if ! code=$(curl -sS --connect-timeout 10 --max-time 60 \
    -o "$response_file" -w "%{http_code}" \
    "https://www.strava.com/api/v3/${endpoint}" \
    -H "Authorization: Bearer ${access}"); then
    die "Strava API 网络请求失败"
  fi
  case "$code" in
    2[0-9][0-9]) ;;
    401|403) die "Strava API HTTP ${code}：请运行 $0 login 重新授权，确保授予 activity:read_all,activity:write" ;;
    404) die "Strava API HTTP 404：活动或轨迹不存在，或当前账号没有访问权限" ;;
    429) die "Strava API HTTP 429：请求次数达到限制，请稍后重试" ;;
    *) die "Strava API HTTP ${code}：读取失败" ;;
  esac
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
do_update() {
  [[ -n "${ACTIVITY_ID:-}" ]] || die "请通过环境变量 ACTIVITY_ID 指定要更新的活动 ID"

  if ! ensure_token; then
    echo "→ 启动浏览器授权..."
    do_login
  fi
  local access
  access=$(token_field access_token)

  # 1) 拉取活动当前内容
  echo "→ 拉取活动 $ACTIVITY_ID 当前内容..."
  curl -s -o /tmp/strava_act.json \
    "https://www.strava.com/api/v3/activities/${ACTIVITY_ID}" \
    -H "Authorization: Bearer ${access}"

  # 2) 计算追加后的 name / description，写到 body 文件
  body_file="$(mktemp -t strava_body).json"
  trap 'rm -f "${body_file:-}"' EXIT
  WEATHER_EMOJI="$WEATHER_EMOJI" WEATHER_DESC="$WEATHER_DESC" \
    python3 - /tmp/strava_act.json "$body_file" <<'PY'
import json, sys, os, re

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
emoji = os.environ["WEATHER_EMOJI"].strip()
block = os.environ["WEATHER_DESC"].strip()

# 标题：去掉末尾已有的天气 emoji（含 variation selector、空格），再追加新 emoji
# 覆盖：符号区 U+2600-U+27BF、象形区 U+1F300-U+1F5FF、补充区 U+1F900-U+1F9FF（含 🥵🥶）
emoji_tail = re.compile(r'[\s️‍☀-➿\U0001F300-\U0001F5FF\U0001F900-\U0001F9FF]+$')
name_base = emoji_tail.sub('', name)
new_name = (name_base + " " + emoji).strip() if emoji else name_base

# 描述：去掉旧的 gearaut 天气块（温度: ... Powered By www.gearaut.com），再追加新块
desc_clean = re.sub(r'温度:.*?Powered By www\.gearaut\.com\s*', '', desc, flags=re.S).strip()
if block:
    new_desc = (desc_clean + "\n" + block) if desc_clean else block
else:
    new_desc = desc_clean

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
  local code
  code=$(curl -s -o /tmp/strava_resp.json -w "%{http_code}" \
    -X PUT "https://www.strava.com/api/v3/activities/${ACTIVITY_ID}" \
    -H "Authorization: Bearer ${access}" \
    -H "Content-Type: application/json; charset=utf-8" \
    --data-binary "@${body_file}")

  echo "→ HTTP 状态码: $code"
  python3 -c '
import json
d=json.load(open("/tmp/strava_resp.json"))
if "id" in d:
    print("✅ 更新成功")
    print("  name       :",d.get("name"))
    print("  description:",d.get("description"))
else:
    print("❌ 失败:",json.dumps(d,ensure_ascii=False,indent=2))
'
}

# ====== 入口 ======
check_config
case "${1:-run}" in
  login) do_login ;;
  auth) do_auth "${2:-}" ;;
  list|get|streams) do_read "$1" "${2:-}" ;;
  show) DRY_RUN=1 do_update ;;
  run|"") do_update ;;
  *) die "未知命令: $1（可用: login / auth <code> / list [数量] / get / streams / show / 不带参数直接运行）" ;;
esac
