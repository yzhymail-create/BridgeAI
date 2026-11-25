#!/usr/bin/env bash
# 记一个邮件回复承诺，让 muse-mail-inbox-watch.sh 定时盯着。
# 用法（确认邮件发出后、立即记）：
#   record-mail-promise.sh --thread-id <来信的 thread_id> --minutes <承诺的分钟数> \
#     [--inbound-id <来信 id>] [--subject <主题>]
# 承诺了几分钟没写明时按 15 分钟记。只在确实发了确认邮件、承诺了
# 时间时才记；没承诺时间的不记。
# 记一条 JSON 行到 $STATE_DIR/promises.jsonl；监测脚本每轮（60 秒）
# 查发件箱：到时间还没发出就叫醒处理方发一封邮件说明（邮箱的事用
# 邮箱反馈，不发微信），之后每分钟继续查，直到发出为止。
set -euo pipefail

STATE_DIR="${MAIL_WATCH_STATE_DIR:-$HOME/hooks/state/muse-mail-inbox-watch}"
PROMISES_FILE="$STATE_DIR/promises.jsonl"

THREAD_ID=""
INBOUND_ID=""
SUBJECT=""
MINUTES=""

while [ $# -gt 0 ]; do
  case "$1" in
    --thread-id) THREAD_ID="$2"; shift 2 ;;
    --inbound-id) INBOUND_ID="$2"; shift 2 ;;
    --subject) SUBJECT="$2"; shift 2 ;;
    --minutes) MINUTES="$2"; shift 2 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

[ -n "$THREAD_ID" ] || { echo "缺 --thread-id" >&2; exit 2; }
# 承诺了几分钟没写明时按 15 分钟记（与头部注释一致）。
[ -n "$MINUTES" ] || MINUTES=15
case "$MINUTES" in
  ''|*[!0-9]*) echo "--minutes 须是正整数分钟" >&2; exit 2 ;;
esac
[ "$MINUTES" -gt 0 ] || { echo "--minutes 须大于 0" >&2; exit 2; }

mkdir -p "$STATE_DIR"
NOW="$(date +%s)"
DEADLINE=$((NOW + MINUTES * 60))
jq -cn --arg thread_id "$THREAD_ID" --arg inbound_id "$INBOUND_ID" \
  --arg subject "$SUBJECT" --argjson minutes "$MINUTES" \
  --argjson promised_at "$NOW" --argjson deadline "$DEADLINE" \
  '{thread_id: $thread_id, inbound_id: $inbound_id, subject: $subject,
    minutes: $minutes, promised_at: $promised_at, deadline: $deadline,
    alerted: false}' >> "$PROMISES_FILE"
echo "已记承诺：thread $THREAD_ID，${MINUTES} 分钟后到期（$(date -d "@$DEADLINE" '+%H:%M')）。"
