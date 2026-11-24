#!/usr/bin/env bash
# 监测 Muse Mail 收件箱：用户（已验证邮箱）发来新邮件时唤醒处理。
# 轻量脚本，只查不办；发现新邮件才叫醒 agent，没新邮件不花 token。
# 首次运行先把现有邮件记为已读，不唤醒（避免旧邮件重复触发）。
# 2026-11-22 修：stdout 与 stderr 分开写——定时轮询环境下 CLI 的
# 附加输出曾混进 JSON 文件，导致启用后每轮解析都失败（试跑环境无此问题）。
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

STATE_DIR="$HOME/hooks/state/muse-mail-inbox-watch"
SEEN_FILE="$STATE_DIR/seen_ids.txt"
mkdir -p "$STATE_DIR"

TMP="$(mktemp)"
TMP_ERR="$(mktemp)"
trap 'rm -f "$TMP" "$TMP_ERR"' EXIT

if ! /opt/hatch/bin/muse-mail messages list --limit 25 > "$TMP" 2>"$TMP_ERR"; then
  log "muse-mail list failed" '{}'
  silent "mail fetch failed"
  exit 0
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
