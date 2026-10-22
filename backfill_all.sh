#!/bin/bash
# 全量历史补扫驱动器：反复跑 ./update_strava_weather.sh sync --all。
# 轮数和退避时间读取共享配置的 backfill 段；授权失效时停止。
# 状态全部落在 .strava_weather_state.json，断点续传。
cd "$(dirname "$0")" || exit 1
set +x
SCRIPT_DIR="$PWD"
source "$SCRIPT_DIR/strava_common.sh"
LOG=backfill-all.log
MAX_ROUNDS="$BACKFILL_MAX_ROUNDS"
round=0
fast429=0
while [ "$round" -lt "$MAX_ROUNDS" ]; do
  round=$((round + 1))
  echo "===== [$(date '+%F %T')] 第 $round/$MAX_ROUNDS 轮 sync --all 开始 =====" >> "$LOG"
  start_ts=$(date +%s)
  ./update_strava_weather.sh sync --all >> "$LOG" 2>&1
  code=$?
  elapsed=$(( $(date +%s) - start_ts ))
  echo "===== [$(date '+%F %T')] 第 $round 轮退出码 $code（用时 ${elapsed}s） =====" >> "$LOG"
  if [ "$code" -eq 4 ]; then
    echo "配额预留暂停，等待 ${BACKFILL_RATE_RETRY_SECONDS}s 后重试" >> "$LOG"
    sleep "$BACKFILL_RATE_RETRY_SECONDS"
    fast429=0
    continue
  fi
  if tail -30 "$LOG" | grep -q "已有 sync 正在运行"; then
    echo "撞锁，等待 ${BACKFILL_LOCK_RETRY_SECONDS}s 后重试" >> "$LOG"
    sleep "$BACKFILL_LOCK_RETRY_SECONDS"
    continue
  fi
  if [ "$code" -eq 0 ]; then
    pending=$(python3 -c "import json;print(len(json.load(open('.strava_weather_state.json')).get('pending',{})))" 2>/dev/null)
    echo "本轮 0 失败，待处理队列: ${pending:-?}" >> "$LOG"
    if [ "$pending" = "0" ]; then
      echo "===== [$(date '+%F %T')] 补扫完成 =====" >> "$LOG"
      break
    fi
    echo "仍有待处理，继续下一轮" >> "$LOG"
    fast429=0
    continue
  fi
  if tail -20 "$LOG" | grep -q "完成授权"; then
    echo "授权失效，停止重试，请手动 login" >> "$LOG"
    break
  fi
  if [ "$elapsed" -lt "$BACKFILL_FAST_FAILURE_SECONDS" ]; then
    fast429=$((fast429 + 1))
  else
    fast429=0
  fi
  if [ "$fast429" -ge "$BACKFILL_FAST_FAILURE_LIMIT" ]; then
    now_utc=$(date -u +%s)
    sleep_for=$(( (now_utc / 86400 + 1) * 86400 - now_utc + BACKFILL_MIDNIGHT_GRACE_SECONDS ))
    echo "连续 ${BACKFILL_FAST_FAILURE_LIMIT} 次快速失败，疑似触及每日上限，休眠 ${sleep_for}s 到 UTC 午夜后继续" >> "$LOG"
    sleep "$sleep_for"
    fast429=0
  else
    echo "等待 ${BACKFILL_RATE_RETRY_SECONDS}s 后重试..." >> "$LOG"
    sleep "$BACKFILL_RATE_RETRY_SECONDS"
  fi
done
echo "===== [$(date '+%F %T')] 驱动器退出 =====" >> "$LOG"
