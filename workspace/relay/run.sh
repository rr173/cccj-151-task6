#!/usr/bin/env bash
# 离线车队回执中继 —— 本地一键启动（仅需 Python 3.9+，无第三方依赖）
set -euo pipefail
cd "$(dirname "$0")"

export RELAY_HOST="${RELAY_HOST:-0.0.0.0}"
export RELAY_PORT="${RELAY_PORT:-8090}"
export RELAY_DB="${RELAY_DB:-$(pwd)/data/relay.db}"
# 默认离线演示桩适配器；接真实主系统时设置：
#   RELAY_CALLBACK_ADAPTER=http RELAY_CALLBACK_URL=http://127.0.0.1:8080/api/relay-callback
export RELAY_CALLBACK_ADAPTER="${RELAY_CALLBACK_ADAPTER:-stub}"
mkdir -p "$(dirname "$RELAY_DB")"

# 可选：启动时播种演示批次（不影响既有数据；设 RELAY_NO_SEED=1 关闭）
if [ "${RELAY_NO_SEED:-0}" != "1" ]; then
  python3 seed_demo.py || true
fi

echo "离线车队回执中继 → http://localhost:${RELAY_PORT}/relay/"
exec python3 backend/server.py
