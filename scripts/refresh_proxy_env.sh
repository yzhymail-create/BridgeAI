#!/usr/bin/env bash
# 从当前 shell 环境刷新 state/proxy.env（出口代理密码会换，bridge.py 每次请求现读这个文件）。
#
# 为什么必须从 cron agent 跑，而不是看门狗 hook 里做：
# 2026-11-24 实测：看门狗 hook 环境里的代理密码到手就是死的——
# 刷新后 1 秒内测就是 407/连接失败；而普通 agent shell 环境里的密码至少活 180 秒。
# hook 脚本跑在“无敏感信息”的环境里，它的代理密码不能用；所以刷新只能走 cron agent。
# 看门狗脚本里原来那段刷新已删掉（留着会把好密码盖成死密码）；刷新只由这个脚本做。
#
# 用法（由 cron 每 2 分钟调一次）：scripts/refresh_proxy_env.sh
# 只在内容变了时写盘，文件权限 0600；环境里没有代理变量时不动旧文件。
set -euo pipefail

BRIDGE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROXY_ENV="$BRIDGE_DIR/state/proxy.env"

if [ -z "${https_proxy:-}${http_proxy:-}${HTTPS_PROXY:-}${HTTP_PROXY:-}" ]; then
  echo "no proxy vars in environment, leaving proxy.env untouched"
  exit 0
fi

tmp_env=$(mktemp)
trap 'rm -f "$tmp_env"' EXIT
{
  for v in http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY no_proxy NO_PROXY; do
    val="${!v:-}"
    [ -n "$val" ] && printf '%s=%s\n' "$v" "$val"
  done
} > "$tmp_env"

if cmp -s "$tmp_env" "$PROXY_ENV" 2>/dev/null; then
  echo "proxy.env already current"
  exit 0
fi

mkdir -p "$(dirname "$PROXY_ENV")"
cp "$tmp_env" "$PROXY_ENV"
chmod 600 "$PROXY_ENV"
echo "proxy.env refreshed"
