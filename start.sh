#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
    echo "[X] 还没安装。请先运行：bash install.sh"
    exit 1
fi

if [ ! -f ".env" ]; then
    echo "[X] 缺少 .env。把 .env.example 复制成 .env 并填好再启动。"
    exit 1
fi

echo "=========================================="
echo "  QQ AI Bot 启动中..."
echo "  控制台: http://127.0.0.1:8080/admin"
echo "  按 Ctrl+C 停止"
echo "=========================================="
echo

exec ./.venv/bin/python bot.py
