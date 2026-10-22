#!/usr/bin/env bash
# 由入口脚本加载；敏感值仅经管道传给网络进程。

set +x
umask 077

die() { echo "❌ $*" >&2; exit 1; }

CONFIG_FILE="$SCRIPT_DIR/config.local.json"
TOKEN_FILE="$SCRIPT_DIR/.strava_tokens.json"

config_exports=$(python3 "$SCRIPT_DIR/project_config.py" shell) || exit 1
eval "$config_exports"
unset config_exports

python3 - "$TOKEN_FILE" <<'PY' || exit 1
import os, stat, sys

for path in sys.argv[1:]:
    if not os.path.lexists(path):
        continue
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise OSError("unsafe credential file")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    except OSError:
        print("❌ 凭据必须为当前用户拥有的普通文件，不能使用符号链接或硬链接", file=sys.stderr)
        sys.exit(1)
PY

token_field() {
  python3 - "$TOKEN_FILE" "$1" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as source:
    print(json.load(source).get(sys.argv[2], ""))
PY
}

save_tokens() {
  printf '%s' "$NEW" | python3 -c '
import json, os, sys, tempfile
path = sys.argv[1]
new = json.load(sys.stdin)
data = {}
if os.path.lexists(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding="utf-8") as source:
        data = json.load(source)
data.update({key: new[key] for key in ("access_token", "refresh_token", "expires_at", "scope") if key in new})
fd, temporary = tempfile.mkstemp(prefix=".strava_tokens.", dir=os.path.dirname(path))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
' "$TOKEN_FILE"
}

oauth_token_request() {
  printf '%s\0' "$CLIENT_ID" "$CLIENT_SECRET" "$1" "$2" |
    python3 -c '
import sys
from urllib.parse import urlencode
client_id, secret, grant, value, _ = sys.stdin.read().split("\0")
field = "code" if grant == "authorization_code" else "refresh_token"
sys.stdout.write(urlencode({"client_id": client_id, "client_secret": secret,
                            "grant_type": grant, field: value}))
' | curl -q -sS --connect-timeout "$HTTP_CONNECT_TIMEOUT_SECONDS" --max-time "$HTTP_REQUEST_TIMEOUT_SECONDS" -X POST \
    "https://www.strava.com/api/v3/oauth/token" --data-binary @-
}

strava_curl() {
  local access="$1"
  shift
  # 记录 API 用量：解析本次调用的方法与路径（best effort，失败不影响请求）
  local method="GET" url="" prev=""
  local arg
  for arg in "$@"; do
    if [[ "$prev" == "-X" ]]; then
      method="$arg"
    fi
    if [[ "$arg" == https://www.strava.com/api/v3/* ]]; then
      url="$arg"
    fi
    prev="$arg"
  done
  printf 'Authorization: Bearer %s\n' "$access" |
    curl -q -sS --connect-timeout "$HTTP_CONNECT_TIMEOUT_SECONDS" --max-time "$HTTP_REQUEST_TIMEOUT_SECONDS" --header @- "$@"
  local rc=$?
  if [[ -n "$url" ]]; then
    { python3 "$SCRIPT_DIR/api_usage.py" log "$method" "/${url#https://www.strava.com/api/v3/}" >/dev/null 2>&1; } || true
  fi
  return $rc
}

check_api_status() {
  case "$1" in
    2[0-9][0-9]) ;;
    401|403) die "Strava API HTTP ${1}：请运行 ./update_strava_weather.sh login 重新授权，确保授予 activity:read_all,activity:write" ;;
    404) die "Strava API HTTP 404：活动或轨迹不存在，或当前账号没有访问权限" ;;
    429) die "Strava API HTTP 429：请求次数达到限制，请稍后重试" ;;
    *) die "Strava API HTTP ${1}：请求失败" ;;
  esac
}

check_config() {
  if [[ -z "${CLIENT_ID:-}" || "$CLIENT_ID" == *"在这里粘贴"* ]]; then
    die "请在 config.local.json 的 strava.client_id 中填好 CLIENT_ID"
  fi
  if [[ -z "${CLIENT_SECRET:-}" || "$CLIENT_SECRET" == *"在这里粘贴"* ]]; then
    die "请在 config.local.json 的 strava.client_secret 中填好 CLIENT_SECRET"
  fi
  return 0
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
  if (( now < expires - TOKEN_REFRESH_MARGIN_SECONDS )); then
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


strava_request() (
  local method="$1" url="$2" body_file="${3:-}"
  require_token >&2
  local access work_dir response_file code
  local -a arguments=(-X "$method")
  access=$(token_field access_token)
  work_dir=$(mktemp -d "${TMPDIR:-/tmp}/strava-request.XXXXXX")
  trap 'rm -rf "$work_dir"' EXIT
  response_file="$work_dir/response.json"
  if [[ -n "$body_file" ]]; then
    arguments+=(-H "Content-Type: application/json; charset=utf-8" --data-binary "@$body_file")
  fi
  if ! code=$(strava_curl "$access" "${arguments[@]}" \
    -o "$response_file" -w "%{http_code}" \
    "$url"); then
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
