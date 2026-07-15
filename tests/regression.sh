#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

test_auth_does_not_require_activity_id() {
  local work_dir="$TMP_DIR/auth"
  local output
  local rc

  mkdir -p "$work_dir"
  cp "$ROOT_DIR/update_strava_weather.sh" "$work_dir/"
  cat > "$work_dir/strava_config.sh" <<'EOF'
CLIENT_ID="test-client-id"
CLIENT_SECRET="test-client-secret"
EOF

  set +e
  output=$(cd "$work_dir" && env -u ACTIVITY_ID ./update_strava_weather.sh auth 2>&1)
  rc=$?
  set -e

  [[ $rc -ne 0 ]] || fail "auth without a code should fail"
  [[ "$output" == *"auth <code>"* ]] || fail "auth was blocked by the ACTIVITY_ID check"
  echo "PASS: auth does not require ACTIVITY_ID"
}

test_reorder_preserves_trailing_text() {
  local work_dir="$TMP_DIR/reorder"
  local output
  local occurrences

  mkdir -p "$work_dir"
  cp "$ROOT_DIR/reorder_summitbag.sh" "$work_dir/"
  cat > "$work_dir/strava_config.sh" <<'EOF'
CLIENT_ID="test-client-id"
CLIENT_SECRET="test-client-secret"
EOF
  cat > "$work_dir/.strava_tokens.json" <<'EOF'
{"access_token":"test-access-token","refresh_token":"test-refresh-token","expires_at":4102444800}
EOF
  cat > "$work_dir/sitecustomize.py" <<'PY'
import json
import urllib.request


class FakeResponse:
    headers = {}

    def __init__(self, body):
        self._body = json.dumps(body, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self._body


def fake_urlopen(request):
    url = request.full_url
    if "/athlete/activities" in url:
        return FakeResponse([{"id": 1, "sport_type": "TrailRun"}])
    if "/activities/1" in url:
        return FakeResponse({
            "id": 1,
            "name": "测试活动",
            "description": (
                "原备注\n"
                "温度: 10°C - 12°C\n"
                "湿度: 60% - 70%\n"
                "Powered By www.gearaut.com\n"
                "活动后的重要备注\n"
                "https://summitbag.com/example"
            ),
        })
    raise AssertionError(f"unexpected URL: {url}")


urllib.request.urlopen = fake_urlopen
PY

  output=$(cd "$work_dir" && PYTHONPATH="$work_dir" ./reorder_summitbag.sh)
  occurrences=$(grep -o "活动后的重要备注" <<< "$output" | wc -l | tr -d ' ')

  [[ "$occurrences" == "2" ]] || fail "reorder deleted unrelated text after the weather block"
  echo "PASS: reorder preserves trailing text"
}

case "${1:-all}" in
  auth) test_auth_does_not_require_activity_id ;;
  reorder) test_reorder_preserves_trailing_text ;;
  all)
    test_auth_does_not_require_activity_id
    test_reorder_preserves_trailing_text
    ;;
  *) fail "unknown test: $1" ;;
esac
