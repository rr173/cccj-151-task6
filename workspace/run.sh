#!/usr/bin/env bash
# 本地一键启动（无需 Docker、无需安装任何依赖，Python 3.9+ 即可）
set -euo pipefail
cd "$(dirname "$0")"

export EVAC_HOST="${EVAC_HOST:-0.0.0.0}"
export EVAC_PORT="${EVAC_PORT:-8080}"
export EVAC_DB="${EVAC_DB:-$(pwd)/data/evac.db}"
mkdir -p "$(dirname "$EVAC_DB")"

echo "山火应急撤离编排系统 → http://localhost:${EVAC_PORT}"
exec python3 backend/server.py
