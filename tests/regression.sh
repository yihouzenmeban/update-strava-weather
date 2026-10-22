#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

prepare_config() {
  cp "$ROOT_DIR/project_config.py" "$ROOT_DIR/config.default.json" "$1/"
  cat > "$1/config.local.json" <<'EOF'
{"strava": {"client_id": "test-client-id", "client_secret": "test-client-secret"}}
EOF
}

test_auth_does_not_require_activity_id() {
  local work_dir="$TMP_DIR/auth"
  local output
  local rc

  mkdir -p "$work_dir"
  cp "$ROOT_DIR/update_strava_weather.sh" "$ROOT_DIR/strava_common.sh" "$ROOT_DIR/api_usage.py" "$work_dir/"
  prepare_config "$work_dir"

  set +e
  output=$(cd "$work_dir" && env -u ACTIVITY_ID ./update_strava_weather.sh auth 2>&1)
  rc=$?
  set -e

  [[ $rc -ne 0 ]] || fail "auth without a code should fail"
  [[ "$output" == *"用法:"*"auth"* ]] || fail "auth was blocked by the ACTIVITY_ID check"
  echo "PASS: auth does not require ACTIVITY_ID"
}

test_reorder_preserves_trailing_text() {
  local work_dir="$TMP_DIR/reorder"
  local output
  local occurrences

  mkdir -p "$work_dir"
  cp "$ROOT_DIR/reorder_summitbag.sh" "$ROOT_DIR/strava_common.sh" "$ROOT_DIR/api_usage.py" "$work_dir/"
  if [[ -f "$ROOT_DIR/weather_template.py" ]]; then
    cp "$ROOT_DIR/weather_template.py" "$work_dir/"
  fi
  prepare_config "$work_dir"
  cat > "$work_dir/.strava_tokens.json" <<'EOF'
{"access_token":"test-access-token","refresh_token":"test-refresh-token","expires_at":4102444800}
EOF
  mkdir -p "$work_dir/bin"
  cat > "$work_dir/bin/curl" <<'PY'
#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
assert sys.stdin.read().strip() == "Authorization: Bearer test-access-token"
url = next(arg for arg in args if arg.startswith("https://"))
if "/athlete/activities" in url:
    payload = [{"id": 1, "sport_type": "TrailRun"}]
elif "/activities/1" in url:
    weather = "温度: 10°C - 12°C\n湿度: 60% - 70%\nPowered By www.gearaut.com\n"
    if os.environ.get("REORDER_CUSTOM"):
        weather = "我的天气 🌙\n气温 -2.0～5.0°C\n"
    payload = {"id": 1, "name": "测试活动", "description": (
        "原备注\n" + weather + "活动后的重要备注\nhttps://summitbag.com/example")}
else:
    raise AssertionError(f"unexpected URL: {url}")
Path(args[args.index("-o") + 1]).write_text(json.dumps(payload))
print("200", end="")
PY
  chmod +x "$work_dir/bin/curl"

  output=$(cd "$work_dir" && PATH="$work_dir/bin:$PATH" ./reorder_summitbag.sh)
  occurrences=$(grep -o "活动后的重要备注" <<< "$output" | wc -l | tr -d ' ')

  [[ "$occurrences" == "2" ]] || fail "reorder deleted unrelated text after the weather block"
  echo "PASS: reorder preserves trailing text"

  cat > "$work_dir/custom.txt" <<'EOF'
我的天气 {emoji}
气温 {t_min:.1f}～{t_max:.1f}°C
EOF
  python3 - "$work_dir/config.local.json" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
config = json.loads(path.read_text())
config["weather"] = {"template_file": "custom.txt"}
path.write_text(json.dumps(config))
PY
  output=$(cd "$work_dir" && PATH="$work_dir/bin:$PATH" REORDER_CUSTOM=1 ./reorder_summitbag.sh)
  [[ "$output" == *"共 1 条活动需要调整"* ]] || fail "reorder did not recognize custom weather"
  occurrences=$(grep -o "活动后的重要备注" <<< "$output" | wc -l | tr -d ' ')
  [[ "$occurrences" == "2" ]] || fail "custom reorder deleted trailing notes"
  [[ "$output" == *$'https://summitbag.com/example\n    | 我的天气 🌙'* ]] || fail "summitbag was not moved before custom weather"
  echo "PASS: reorder recognizes custom templates and preserves trailing text"

  output=$(cd "$work_dir" && PATH="$work_dir/bin:$PATH" REORDER_SUMMITBAG=0 YES=1 ./reorder_summitbag.sh)
  [[ "$output" == *"山峰排序已关闭"* && "$output" != *"拉取活动列表"* ]] || fail "disabled reorder still queried activities"
  echo "PASS: standalone reorder can be disabled before API requests"
}

test_read_commands() {
  local work_dir="$TMP_DIR/read"
  mkdir -p "$work_dir/bin"
  cp "$ROOT_DIR/update_strava_weather.sh" "$ROOT_DIR/strava_common.sh" "$ROOT_DIR/api_usage.py" "$work_dir/"
  prepare_config "$work_dir"
  cat > "$work_dir/.strava_tokens.json" <<'EOF'
{"access_token":"test-access-token","refresh_token":"test-refresh-token","expires_at":4102444800}
EOF
  cat > "$work_dir/bin/curl" <<'PY'
#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs

args = sys.argv[1:]
input_data = sys.stdin.read()
url = next(arg for arg in args if arg.startswith("https://"))
method = args[args.index("-X") + 1] if "-X" in args else "GET"
with open("requests.jsonl", "a") as log:
    log.write(json.dumps({"url": url, "method": method}) + "\n")

if url.endswith("/oauth/token"):
    assert method == "POST"
    form = parse_qs(input_data)
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == ["test-refresh-token"]
    print(json.dumps({"access_token": "refreshed-access-token",
                      "refresh_token": "refreshed-refresh-token",
                      "expires_at": 4102444800}))
    sys.exit(0)

assert method == "GET", "read command must use GET"
assert args[args.index("--header") + 1] == "@-"
assert input_data.strip() == "Authorization: Bearer " + os.environ.get(
    "EXPECTED_ACCESS", "test-access-token")
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "network":
    sys.exit(7)
status = mode if mode.isdigit() else "200"
activity = {"id": 123, "name": "晨跑", "start_date_local": "2026-10-15T07:00:00Z",
            "moving_time": 1800, "start_latlng": [39.9, 116.4]}
if "/athlete/activities?" in url:
    body = [] if mode == "empty" else [activity]
elif url.endswith("/activities/123"):
    body = dict(activity, description="原备注")
elif url.endswith("/activities/123/streams?keys=latlng&key_by_type=true"):
    body = {"latlng": {"data": [[39.9, 116.4], [39.91, 116.41]]}}
else:
    raise AssertionError(f"unexpected URL: {url}")
if status != "200":
    body = {"message": "Request failed"}
payload = "not json" if mode == "invalid-json" else json.dumps(body, ensure_ascii=False)
Path(args[args.index("-o") + 1]).write_text(payload)
assert "-w" in args
print(status, end="")
PY
  chmod +x "$work_dir/bin/curl"

  (
    cd "$work_dir"
    export PATH="$work_dir/bin:$PATH"
    unset ACTIVITY_ID STRAVA_PAGE STRAVA_AFTER STRAVA_BEFORE

    ./update_strava_weather.sh list > list.json 2> stderr.txt
    ./update_strava_weather.sh list 5 > list-five.json 2> stderr.txt
    ACTIVITY_ID=123 ./update_strava_weather.sh get > activity.json 2> stderr.txt
    ACTIVITY_ID=123 ./update_strava_weather.sh streams > streams.json 2> stderr.txt
    FAKE_MODE=empty ./update_strava_weather.sh list > empty.json 2> stderr.txt
    STRAVA_PAGE=3 STRAVA_AFTER=100 STRAVA_BEFORE=200 ./update_strava_weather.sh list 50 > page.json 2> stderr.txt
    python3 - <<'PY'
import json
from pathlib import Path

assert json.loads(Path("list.json").read_text())[0]["id"] == 123
assert json.loads(Path("activity.json").read_text())["description"] == "原备注"
assert json.loads(Path("streams.json").read_text())["latlng"]["data"][0] == [39.9, 116.4]
assert json.loads(Path("empty.json").read_text()) == []
requests = [json.loads(line) for line in Path("requests.jsonl").read_text().splitlines()]
assert requests[0]["url"].endswith("/athlete/activities?page=1&per_page=1")
assert requests[1]["url"].endswith("/athlete/activities?page=1&per_page=5")
assert requests[-1]["url"].endswith("/athlete/activities?page=3&per_page=50&after=100&before=200")
PY
    echo "PASS: read commands return API JSON with the expected endpoints"

    python3 - <<'PY'
import json
from pathlib import Path
path = Path(".strava_tokens.json")
tokens = json.loads(path.read_text())
tokens["expires_at"] = 0
path.write_text(json.dumps(tokens))
PY
    EXPECTED_ACCESS=refreshed-access-token ./update_strava_weather.sh list > refreshed.json 2> stderr.txt
    python3 - <<'PY'
import json
from pathlib import Path
assert json.loads(Path("refreshed.json").read_text())[0]["id"] == 123
tokens = json.loads(Path(".strava_tokens.json").read_text())
assert tokens["refresh_token"] == "refreshed-refresh-token"
requests = [json.loads(line) for line in Path("requests.jsonl").read_text().splitlines()]
assert requests[-2]["url"].endswith("/oauth/token")
assert requests[-1]["method"] == "GET"
PY
    export EXPECTED_ACCESS=refreshed-access-token
    echo "PASS: read commands refresh and persist expired tokens without polluting JSON"

    local mode
    for mode in 401 403 404 429 500 invalid-json network; do
      if FAKE_MODE="$mode" ./update_strava_weather.sh list > failed.json 2> stderr.txt; then
        cat stderr.txt >&2
        cat failed.json >&2
        fail "read command should fail for $mode"
      fi
      [[ ! -s failed.json ]] || fail "failed read returned success output for $mode"
      [[ -s stderr.txt ]] || fail "failed read omitted error for $mode"
      if [[ "$mode" =~ ^[0-9]+$ ]]; then
        [[ "$(cat stderr.txt)" == *"HTTP $mode"* ]] || fail "missing HTTP status for $mode"
      fi
    done
    echo "PASS: API, network and JSON failures return nonzero with stderr diagnostics"

    cp requests.jsonl requests-before.jsonl
    local count
    for count in 0 -1 201 abc '1&after=0'; do
      if ./update_strava_weather.sh list "$count" > failed.json 2> stderr.txt; then
        fail "list should reject count $count"
      fi
    done
    local command activity_id
    for command in get streams; do
      for activity_id in '' abc '123?bad=1'; do
        if ACTIVITY_ID="$activity_id" ./update_strava_weather.sh "$command" > failed.json 2> stderr.txt; then
          fail "$command should reject invalid ACTIVITY_ID"
        fi
      done
    done
    local variable value
    for variable in STRAVA_PAGE STRAVA_AFTER STRAVA_BEFORE; do
      for value in -1 '1&bad=1'; do
        if env "$variable=$value" ./update_strava_weather.sh list > failed.json 2> stderr.txt; then
          fail "list should reject $variable=$value"
        fi
      done
    done
    cmp -s requests-before.jsonl requests.jsonl || fail "invalid arguments made API requests"
    echo "PASS: invalid counts and activity IDs fail before API requests"
  )
}

case "${1:-all}" in
  auth) test_auth_does_not_require_activity_id ;;
  reorder) test_reorder_preserves_trailing_text ;;
  read) test_read_commands ;;
  security) python3 "$ROOT_DIR/tests/security.py" ;;
  config) python3 "$ROOT_DIR/tests/config.py" ;;
  sync) python3 "$ROOT_DIR/tests/sync.py" ;;
  api_usage) python3 "$ROOT_DIR/tests/api_usage.py" ;;
  peaks) python3 "$ROOT_DIR/tests/peaks.py" ;;
  templates) python3 "$ROOT_DIR/tests/templates.py" ;;
  activity_update) python3 "$ROOT_DIR/tests/activity_update.py" ;;
  webhook) python3 "$ROOT_DIR/tests/webhook.py" ;;
  all)
    python3 "$ROOT_DIR/tests/config.py"
    test_auth_does_not_require_activity_id
    test_reorder_preserves_trailing_text
    test_read_commands
    python3 "$ROOT_DIR/tests/templates.py"
    python3 "$ROOT_DIR/tests/activity_update.py"
    python3 "$ROOT_DIR/tests/security.py"
    python3 "$ROOT_DIR/tests/sync.py"
    python3 "$ROOT_DIR/tests/api_usage.py"
    python3 "$ROOT_DIR/tests/peaks.py"
    python3 "$ROOT_DIR/tests/webhook.py"
    ;;
  *) fail "unknown test: $1" ;;
esac
