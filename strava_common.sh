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
  printf 'Authorization: Bearer %s\n' "$access" |
    curl -q -sS --connect-timeout "$HTTP_CONNECT_TIMEOUT_SECONDS" --max-time "$HTTP_REQUEST_TIMEOUT_SECONDS" --header @- "$@"
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
