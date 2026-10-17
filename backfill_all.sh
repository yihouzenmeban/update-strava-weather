#!/bin/bash
# 全量历史补扫驱动器：反复跑 ./update_strava_weather.sh sync --all。
# 退避策略（Strava 限流 400/15分钟、4000/天）：
# - 429：睡 20 分钟（15 分钟窗口已恢复）后重试；
# - 连续 3 次"启动 5 分钟内就 429"：判定触及每日上限，休眠到 UTC 午夜 +15 分钟；
# - 授权失效：停下报错；撞锁（另有 sync 在跑）：睡 10 分钟。
# 状态全部落在 .strava_weather_state.json，断点续传。
cd "$(dirname "$0")" || exit 1
LOG=backfill-all.log
MAX_ROUNDS=200
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
  if tail -30 "$LOG" | grep -q "已有 sync 正在运行"; then
    echo "撞锁，睡 10 分钟后重试" >> "$LOG"
    sleep 600
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
  if [ "$elapsed" -lt 300 ]; then
    fast429=$((fast429 + 1))
  else
    fast429=0
  fi
  if [ "$fast429" -ge 3 ]; then
    now_utc=$(date -u +%s)
    sleep_for=$(( (now_utc / 86400 + 1) * 86400 - now_utc + 900 ))
    echo "连续 3 次快速 429，疑似触及每日上限，休眠 ${sleep_for}s 到 UTC 午夜后继续" >> "$LOG"
    sleep "$sleep_for"
    fast429=0
  else
    echo "等待 20 分钟后重试（15 分钟窗口退避）..." >> "$LOG"
    sleep 1200
  fi
done
echo "===== [$(date '+%F %T')] 驱动器退出 =====" >> "$LOG"
