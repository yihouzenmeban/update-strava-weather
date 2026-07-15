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
# 依赖同目录 strava_config.sh（CLIENT_ID/CLIENT_SECRET）和 .strava_tokens.json
# （由 update_strava_weather.sh login 生成）。

set -euo pipefail

# ====== 配置区 ======
CONFIG_FILE="$(dirname "$0")/strava_config.sh"
if [[ -f "$CONFIG_FILE" ]]; then
  # shellcheck source=strava_config.sh
  source "$CONFIG_FILE"
fi
TOKEN_FILE="$(dirname "$0")/.strava_tokens.json"
# ====================

die() { echo "❌ $*" >&2; exit 1; }

if [[ -z "${CLIENT_ID:-}" || "$CLIENT_ID" == *"在这里粘贴"* ]]; then
  die "请先创建 strava_config.sh 并填好 CLIENT_ID"
fi
if [[ -z "${CLIENT_SECRET:-}" || "$CLIENT_SECRET" == *"在这里粘贴"* ]]; then
  die "请先创建 strava_config.sh 并填好 CLIENT_SECRET"
fi
[[ -f "$TOKEN_FILE" ]] || die "没有令牌缓存 $TOKEN_FILE，请先跑 ./update_strava_weather.sh login 授权"

# 读缓存里的某个字段
token_field() { python3 -c "import json,sys;print(json.load(open('$TOKEN_FILE')).get('$1',''))"; }

# 保存刷新后的令牌
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

# 确保拿到未过期的 access_token：有效直接用，过期就刷新；刷新失败提示重新授权
ensure_token() {
  local access refresh expires now
  access=$(token_field access_token)
  refresh=$(token_field refresh_token)
  expires=$(token_field expires_at)
  [[ -n "$access" && -n "$refresh" ]] || die "令牌缓存不完整，请跑 ./update_strava_weather.sh login 重新授权"

  now=$(date +%s)
  if (( now < expires - 60 )); then
    echo "→ 令牌仍有效，跳过刷新"
    return 0
  fi

  echo "→ 令牌已过期，刷新中..."
  local resp
  resp=$(curl -s -X POST "https://www.strava.com/oauth/token" \
    -d "client_id=${CLIENT_ID}" \
    -d "client_secret=${CLIENT_SECRET}" \
    -d "refresh_token=${refresh}" \
    -d "grant_type=refresh_token")
  if ! echo "$resp" | python3 -c '
import json,sys
d=json.load(sys.stdin)
if "access_token" not in d:
    print("→ 刷新失败，请跑 ./update_strava_weather.sh login 重新授权");sys.exit(1)
print("✅ 已刷新")
'; then
    return 1
  fi
  NEW=$(echo "$resp" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(json.dumps({k:d[k] for k in ("access_token","refresh_token","expires_at","scope") if k in d}))')
  save_tokens
  return 0
}

ensure_token

ACCESS=$(token_field access_token)
YES="${YES:-}"
DRY_RUN="${DRY_RUN:-}"
# 默认仅预览；YES=1 才写入
APPLY=0
[[ "$YES" == "1" ]] && APPLY=1

ACCESS="$ACCESS" APPLY="$APPLY" python3 - <<'PY'
import json, os, sys, time, urllib.request, urllib.error

ACCESS = os.environ["ACCESS"]
APPLY = os.environ["APPLY"] == "1"
HEADERS = {"Authorization": f"Bearer {ACCESS}"}

def req(method, url, data=None):
    """发请求，返回 (parsed_body, rateLimit_remaining_15min, rateLimit_limit_15min)。
    剩余配额不足时自动 sleep 等待。"""
    while True:
        r = urllib.request.Request(url, method=method, headers=HEADERS)
        if data is not None:
            r.add_header("Content-Type", "application/json; charset=utf-8")
            r.data = data.encode("utf-8")
        try:
            resp = urllib.request.urlopen(r)
            body = resp.read().decode("utf-8")
            rem = resp.headers.get("X-RateLimit-Remaining", "")
            lim = resp.headers.get("X-RateLimit-Limit", "")
            # 15 分钟窗口剩余 < 5 就等到补满（按 900s 窗口估算）
            try:
                rem15 = int(rem.split(",")[0])
            except Exception:
                rem15 = 999
            if rem15 < 5:
                print(f"  ⏳ 限流剩余 {rem15}，等待 60s...", flush=True)
                time.sleep(60)
                continue
            return json.loads(body) if body else {}, rem, lim
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print("  ⏳ 触发限流 429，等待 60s...", flush=True)
                time.sleep(60)
                continue
            if e.code in (502, 503, 504):
                print(f"  ⏳ HTTP {e.code}，10s 后重试...", flush=True)
                time.sleep(10)
                continue
            err = e.read().decode("utf-8", "replace")
            print(f"  ❌ HTTP {e.code}: {err[:200]}", flush=True)
            return None, "", ""
        except urllib.error.URLError as e:
            # 瞬时网络/SSL 错误，退避重试
            print(f"  ⏳ 网络错误 {e}，10s 后重试...", flush=True)
            time.sleep(10)
            continue

WEATHER_MARK = "Powered By www.gearaut.com"
SUMMIT_MARK = "summitbag.com"

# 1) 分页拉全部活动，过滤 TrailRun
print("→ 拉取活动列表...", flush=True)
trails = []
page = 1
while True:
    batch, _, _ = req("GET", f"https://www.strava.com/api/v3/athlete/activities?per_page=200&page={page}")
    if not batch:
        break
    trails.extend(a for a in batch if a.get("sport_type") == "TrailRun")
    if len(batch) < 200:
        break
    page += 1
    time.sleep(0.3)
print(f"→ 共 {len(trails)} 条越野跑活动，逐条拉取描述...", flush=True)

# 2) 逐条拉详情，检测并计算新描述
changes = []  # [{id, name, before, after}]
for i, a in enumerate(trails, 1):
    aid = a["id"]
    detail, _, _ = req("GET", f"https://www.strava.com/api/v3/activities/{aid}")
    time.sleep(0.3)
    if not detail:
        continue
    desc = detail.get("description") or ""
    if not desc:
        continue
    wb_pos = desc.find(WEATHER_MARK)
    sb_pos = desc.find(SUMMIT_MARK)
    # 需要同时有天气块和 summitbag，且 summitbag 在天气之后
    if wb_pos == -1 or sb_pos == -1 or sb_pos < wb_pos:
        continue

    # 目标顺序：备注 → summitbag → 天气块，其它内容和空行保持原顺序
    lines = desc.split("\n")
    wb_start = wb_end = None
    for idx, l in enumerate(lines):
        if wb_start is None and l.startswith("温度:"):
            wb_start = idx
        if "Powered By www.gearaut.com" in l:
            wb_end = idx
            break
    summit_lines = [l for l in lines if SUMMIT_MARK in l]
    if wb_start is None or wb_end is None or not summit_lines:
        continue
    # summitbag 已确认位于天气块之后，移除后插到天气块起始位置即可
    other_lines = [l for l in lines if SUMMIT_MARK not in l]
    new_desc = "\n".join(other_lines[:wb_start] + summit_lines + other_lines[wb_start:])
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
    res, _, _ = req("PUT", f"https://www.strava.com/api/v3/activities/{c['id']}", data=body)
    if res and "id" in res:
        ok += 1
        print(f"  ✅ {c['id']} {c['name']}", flush=True)
    else:
        print(f"  ❌ {c['id']} {c['name']} 失败", flush=True)
    time.sleep(0.5)
print(f"\n完成：{ok}/{len(changes)} 条已更新")
PY
