#!/usr/bin/env bash
# 把越野跑活动描述里的 summitbag 行提到天气块前面
#
# 背景：天气脚本会把天气块追加到描述末尾，summitbag.com 又会把 summitbag 行
# 追加到末尾，导致 summitbag 排在天气之后。本脚本把 summitbag 行挪到天气块
# 前面，最终顺序为：备注 → summitbag → 天气块。
#
# 范围：仅处理 sport_type == TrailRun 的活动。
#
# 用法：
#   ./reorder_summitbag.sh           预览将要调整的活动（不改 Strava）
#   YES=1 ./reorder_summitbag.sh     预览后直接写入
#   DRY_RUN=1 ./reorder_summitbag.sh 等同默认预览
#
# 依赖同目录 config.local.json（strava 应用密钥）和 .strava_tokens.json
# （由 update_strava_weather.sh login 生成）。

set +x
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=strava_common.sh
source "$SCRIPT_DIR/strava_common.sh"
[[ "$REORDER_SUMMITBAG" == "0" || "$REORDER_SUMMITBAG" == "1" ]] || die "REORDER_SUMMITBAG 必须为 0 或 1"
if [[ "$REORDER_SUMMITBAG" == "0" ]]; then
  echo "⏭️ 山峰排序已关闭（REORDER_SUMMITBAG=0）"
  exit 0
fi

export STRAVA_NONINTERACTIVE=1
check_config
require_token

YES="${YES:-}"
DRY_RUN="${DRY_RUN:-}"
# 默认仅预览；YES=1 才写入
APPLY=0
[[ "$YES" == "1" && "$DRY_RUN" != "1" ]] && APPLY=1

WEATHER_TEMPLATE_FILE="${WEATHER_TEMPLATE_FILE:-}" APPLY="$APPLY" python3 - "$SCRIPT_DIR" <<'PY'
import json, os, re, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from weather_template import load_template, reorder_summitbag
from project_config import load_config
from api_usage import UsageTracker

config = load_config()
settings = config["reorder"]

try:
    template = load_template(os.environ.get("WEATHER_TEMPLATE_FILE"))
except ValueError as error:
    print(f"❌ {error}", file=sys.stderr)
    sys.exit(1)

APPLY = os.environ["APPLY"] == "1"
budget = dict(config["rate_limit"])
budget["reserve_read_15min"] = max(budget["reserve_read_15min"], settings["rate_limit_floor"])
usage = UsageTracker(config=budget)
REQUEST_SCRIPT = ('set -euo pipefail; SCRIPT_DIR=$1; source "$SCRIPT_DIR/strava_common.sh"; '
                  'strava_request "$2" "$3" "${4:-}"')

def req(method, url, data=None):
    """Use the shared authenticated transport, usage log and bulk budget."""
    while True:
        ok, reason = usage.check_budget(bulk=True)
        if not ok:
            print(f"⏸️ {reason}", file=sys.stderr)
            sys.exit(4)
        with tempfile.TemporaryDirectory(prefix="strava-reorder-") as directory:
            body = Path(directory) / "body.json"
            if data is not None:
                body.write_text(data, encoding="utf-8")
            result = subprocess.run(
                ["bash", "-c", REQUEST_SCRIPT, "strava-request", sys.argv[1], method, url,
                 str(body) if data is not None else ""],
                text=True, capture_output=True)
        if result.returncode == 0:
            return json.loads(result.stdout)
        error = result.stderr.strip()
        status = re.search(r"HTTP ([0-9]{3})", error)
        code = int(status[1]) if status else None
        if code == 429:
            delay = settings["rate_retry_seconds"]
        elif code in (502, 503, 504) or "网络请求失败" in error:
            delay = settings["error_retry_seconds"]
        else:
            print(error or "❌ Strava 请求失败", file=sys.stderr)
            sys.exit(1)
        print(f"  ⏳ 请求失败，{delay}s 后重试...", flush=True)
        time.sleep(delay)

# 1) 分页拉全部活动，过滤 TrailRun
print("→ 拉取活动列表...", flush=True)
trails = []
page = 1
while True:
    batch = req("GET", f"https://www.strava.com/api/v3/athlete/activities?per_page={settings['page_size']}&page={page}")
    if not batch:
        break
    trails.extend(a for a in batch if a.get("sport_type") == "TrailRun")
    if len(batch) < settings["page_size"]:
        break
    page += 1
    time.sleep(settings["request_interval_milliseconds"] / 1000)
print(f"→ 共 {len(trails)} 条越野跑活动，逐条拉取描述...", flush=True)

# 2) 逐条拉详情，检测并计算新描述
changes = []  # [{id, name, before, after}]
for i, a in enumerate(trails, 1):
    aid = a["id"]
    detail = req("GET", f"https://www.strava.com/api/v3/activities/{aid}")
    time.sleep(settings["request_interval_milliseconds"] / 1000)
    if not detail:
        continue
    desc = detail.get("description") or ""
    if not desc:
        continue
    new_desc = reorder_summitbag(desc, template)
    if new_desc == desc:
        continue
    changes.append({"id": aid, "name": detail.get("name", ""), "before": desc, "after": new_desc})
    print(f"  [{i}/{len(trails)}] {aid} {detail.get('name','')} → 需调整", flush=True)

print()
print("=" * 70)
print(f"共 {len(changes)} 条活动需要调整（summitbag 挪到天气块前）")
print("=" * 70)
for c in changes:
    print(f"\n[{c['id']}] {c['name']}")
    print("  原描述:")
    for l in c["before"].split("\n"):
        print(f"    | {l}")
    print("  新描述:")
    for l in c["after"].split("\n"):
        print(f"    | {l}")

if not changes:
    print("\n✅ 没有需要调整的活动")
    sys.exit(0)

if not APPLY:
    print(f"\n→ 以上为预览。确认无误后用 YES=1 ./reorder_summitbag.sh 写入")
    sys.exit(0)

# 3) 写入
print(f"\n→ 开始写入 {len(changes)} 条...", flush=True)
ok = 0
for c in changes:
    body = json.dumps({"description": c["after"]}, ensure_ascii=False)
    res = req("PUT", f"https://www.strava.com/api/v3/activities/{c['id']}", data=body)
    if res and "id" in res:
        ok += 1
        print(f"  ✅ {c['id']} {c['name']}", flush=True)
    else:
        print(f"  ❌ {c['id']} {c['name']} 失败", flush=True)
    time.sleep(settings["write_interval_milliseconds"] / 1000)
print(f"\n完成：{ok}/{len(changes)} 条已更新")
PY
