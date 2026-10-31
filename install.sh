#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "=========================================="
echo "  QQ AI Bot - 安装"
echo "=========================================="

PY=""
for candidate in python3.12 python3.13 python3.14 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
            PY="$candidate"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "[X] 没找到 Python 3.12+。请先安装：https://www.python.org/downloads/"
    exit 1
fi
echo "使用 $($PY -V)"

if [ ! -x ".venv/bin/python" ]; then
    echo "[1/3] 创建虚拟环境 .venv ..."
    "$PY" -m venv .venv
else
    echo "[1/3] 虚拟环境已存在，跳过。"
fi

echo "[2/3] 安装依赖 ..."
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt

if [ ! -f ".env" ]; then
    echo "[3/3] 生成 .env ..."
    cp .env.example .env
    echo
    echo "[!] 请先编辑 .env，至少填这 4 项："
    echo "    SUPERUSERS        你自己的 QQ 号（不是机器人号）"
    echo "    NICKNAME          机器人名字"
    echo "    LLM_API_KEY       模型 API Key"
    echo "    ADMIN_TOKEN       控制台登录口令"
    echo
    echo "    LLM_PROVIDER / LLM_BASE_URL / LLM_MODEL 用下面的向导填最省事："
    echo "    .venv/bin/python scripts/setup_wizard.py"
    echo
    echo "    编辑命令：nano .env"
else
    echo "[3/3] .env 已存在，跳过。"
fi

echo
echo "安装完成。配置好 .env 后运行：bash start.sh"
