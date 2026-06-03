#!/bin/bash
# 一键启动 mom_agent
# 用法: ./start.sh

set -e

cd "$(dirname "$0")"

echo ">>> 检查依赖服务 (PostgreSQL + Redis)..."
docker compose up -d

echo ">>> 检查配置文件..."
if [ ! -f .env ]; then
    echo ">>> 复制 .env.example -> .env"
    cp .env.example .env
    echo ""
    echo "!!! 请先编辑 .env 填入 vLLM 和 RAGFlow 配置"
    echo "!!! 配置完成后重新运行: ./start.sh"
    exit 1
fi

echo ">>> 安装依赖..."
pip install -r requirements.txt -q

echo ">>> 启动服务 http://localhost:8000"
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
