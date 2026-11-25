#!/usr/bin/env bash
# 监测 Muse Mail 收件箱：用户（已验证邮箱）发来新邮件时唤醒处理。
# 轻量脚本，只查不办；发现新邮件才叫醒 agent，没新邮件不花 token。
# 首次运行先把现有邮件记为已读，不唤醒（避免旧邮件重复触发）。
# 2026-11-22 修：stdout 与 stderr 分开写——定时轮询环境下 CLI 的
# 附加输出曾混进 JSON 文件，导致启用后每轮解析都失败（试跑环境无此问题）。
# 2026-11-25 加：承诺定时检查。回确认邮件、承诺了几分钟后反馈时，
# 处理方用 record-mail-promise.sh 记下承诺（thread_id、承诺分钟数）；
# 本脚本每轮（60 秒）先查承诺：到时间还没在发件箱看到结果邮件，
# 就叫醒处理方，给用户发一封邮件说明还没发出（邮箱的事用邮箱反馈，
# 不发微信），之后每分钟继续查，直到发出为止才停。
# 判定“已发出”：同一 thread 里、确认邮件发出 60 秒之后有 OUTBOUND
# 邮件。60 秒宽限是排除确认邮件本身（服务器时间与本地记账时间
# 可能差几秒）；承诺了几分钟的活，结果不可能 60 秒内就发出来。
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

STATE_DIR="${MAIL_WATCH_STATE_DIR:-$HOME/hooks/state/muse-mail-inbox-watch}"
SEEN_FILE="$STATE_DIR/seen_ids.txt"
PROMISES_FILE="$STATE_DIR/promises.jsonl"
mkdir -p "$STATE_DIR"

TMP="$(mktemp)"
TMP_ERR="$(mktemp)"
trap 'rm -f "$TMP" "$TMP_ERR"' EXIT

if ! /opt/hatch/bin/muse-mail messages list --limit 25 > "$TMP" 2>"$TMP_ERR"; then
  log "muse-mail list failed" '{}'
  silent "mail fetch failed"
  exit 0
fi

# ---- 承诺定时检查：先于收件箱检查跑，wake 会直接退出脚本 ----
# 返回：0=本轮无须叫醒；1=须叫醒（叫醒处理方给用户发邮件说明）。
# 全局变量 WAKE_PAYLOAD 供调用方在承诺文件落盘后再 wake。
WAKE_PAYLOAD=""
check_promises() {
  [ -f "$PROMISES_FILE" ] || return 0
  [ -s "$PROMISES_FILE" ] || return 0
  local now tmp_promises
  now="$(date +%s)"
  tmp_promises="$STATE_DIR/.promises.tmp"
  : > "$tmp_promises"
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    local thread_id promised_at deadline alerted subject minutes
    thread_id="$(printf '%s' "$line" | jq -r '.thread_id // empty' 2>/dev/null || true)"
    promised_at="$(printf '%s' "$line" | jq -r '.promised_at // 0' 2>/dev/null || true)"
    deadline="$(printf '%s' "$line" | jq -r '.deadline // 0' 2>/dev/null || true)"
    alerted="$(printf '%s' "$line" | jq -r '.alerted // false' 2>/dev/null || true)"
    subject="$(printf '%s' "$line" | jq -r '.subject // ""' 2>/dev/null || true)"
    minutes="$(printf '%s' "$line" | jq -r '.minutes // 0' 2>/dev/null || true)"
    if [ -z "$thread_id" ] || [ "$promised_at" = "0" ] || [ "$deadline" = "0" ]; then
      log "mail promise bad line skipped" '{}'
      continue
    fi
    # 已发出？同一 thread、确认邮件 60 秒之后有发出的邮件即算发出。
    if jq -e --arg t "$thread_id" --argjson pa "$promised_at" \
        '.data[] | select(.direction=="OUTBOUND" and .thread_id==$t and .created_at > ($pa + 60))' \
        "$TMP" >/dev/null 2>&1; then
      log "mail promise fulfilled" "{\"thread_id\":\"$thread_id\"}"
      continue
    fi
    # 还没到时间：留着，下轮再查。
    if [ "$now" -lt "$deadline" ]; then
      printf '%s\n' "$line" >> "$tmp_promises"
      continue
    fi
    # 到时间还没发出：只叫醒一次（发邮件说明），之后每分钟继续查、
    # 直到发出为止。邮箱的事用邮箱反馈，不发微信。
    if [ "$alerted" = "true" ]; then
      printf '%s\n' "$line" >> "$tmp_promises"
      continue
    fi
    local overdue_min
    overdue_min=$(( (now - deadline) / 60 ))
    printf '%s\n' "$line" | jq -c '.alerted = true' >> "$tmp_promises"
    WAKE_PAYLOAD="$(jq -cn --arg t "$thread_id" --arg s "$subject" --argjson m "$minutes" --argjson o "$overdue_min" \
      '{overdue_mail: {thread_id: $t, subject: $s, promised_minutes: $m, overdue_minutes: $o}, note: "promised email result still unsent; send the user a brief status email in this thread saying it is delayed (and why, if known)"}')"
    log "mail promise overdue, wake to email user" "{\"thread_id\":\"$thread_id\"}"
  done < "$PROMISES_FILE"
  if [ "${HATCH_HOOK_DRY_RUN:-0}" != "1" ]; then
    mv -f "$tmp_promises" "$PROMISES_FILE"
  fi
  if [ -n "$WAKE_PAYLOAD" ]; then
    return 1
  fi
  return 0
}

if ! check_promises; then
  wake "mail reply overdue" "$WAKE_PAYLOAD"
fi

# 用户（OWNER）发来的收件箱邮件 ID
CURRENT_FILE="$STATE_DIR/.current_ids"
if ! jq -r '.data[] | select(.direction=="INBOUND" and .sender_relationship=="OWNER") | .id' "$TMP" | sort -u > "$CURRENT_FILE"; then
  head -c 2000 "$TMP" > "$STATE_DIR/last_bad_output.txt"
  head -c 500 "$TMP_ERR" > "$STATE_DIR/last_bad_stderr.txt"
  log "muse-mail list parse failed" '{}'
  silent "mail parse failed"
  exit 0
fi

if [ ! -f "$SEEN_FILE" ]; then
  if [ "${HATCH_HOOK_DRY_RUN:-0}" != "1" ]; then
    cp "$CURRENT_FILE" "$SEEN_FILE"
  fi
  COUNT="$(wc -l < "$CURRENT_FILE" | tr -d ' ')"
  silent "first run, baselined ${COUNT} owner messages"
  exit 0
fi

NEW_FILE="$STATE_DIR/.new_ids"
comm -13 <(sort -u "$SEEN_FILE") "$CURRENT_FILE" > "$NEW_FILE" || true

if [ ! -s "$NEW_FILE" ]; then
  silent "no new owner mail"
  exit 0
fi

if [ "${HATCH_HOOK_DRY_RUN:-0}" != "1" ]; then
  sort -u "$SEEN_FILE" "$NEW_FILE" > "$SEEN_FILE.tmp"
  mv "$SEEN_FILE.tmp" "$SEEN_FILE"
fi

IDS="$(cat "$NEW_FILE")"
PAYLOAD="$(jq -c --arg ids "$IDS" '
  {new_messages: [.data[] | select(.id as $id | ($ids | split("\n")) | index($id)) | {id, from: .header_from, subject, date}]}' "$TMP")"
wake "new owner mail" "$PAYLOAD"
