@echo off
:: 一键启动 mom_agent
:: 用法: start.bat

cd /d "%~dp0"

echo. & echo >>> 检查依赖服务 (PostgreSQL + Redis)...
docker compose up -d

echo. & echo >>> 检查配置文件...
if not exist .env (
    echo >>> 复制 .env.example -^> .env
    copy .env.example .env
    echo.
    echo !!! 请先编辑 .env 填入 vLLM 和 RAGFlow 配置
    echo !!! 配置完成后重新运行: start.bat
    pause
    exit /b 1
)

echo. & echo >>> 安装依赖...
pip install -r requirements.txt -q

echo. & echo >>> 启动服务 http://localhost:8000
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
pause
