@echo off
rem Windows 一键启动后端（PowerShell / cmd 均可双击或命令行运行）
rem 首次运行自动创建 venv 并安装依赖；之后直接启动 uvicorn 热重载（端口 8000）。
rem 前端请另开终端：cd frontend ^&^& npm install ^&^& npm run dev
setlocal
cd /d "%~dp0"

if not exist data\originals mkdir data\originals
if not exist data\pdfs mkdir data\pdfs

if not exist backend\venv (
    echo [start] creating backend venv ...
    python -m venv backend\venv || goto :err
    backend\venv\Scripts\python -m pip install --upgrade pip || goto :err
    rem 国内网络较慢时可在行尾追加：-i https://pypi.tuna.tsinghua.edu.cn/simple
    backend\venv\Scripts\python -m pip install -r backend\requirements.txt || goto :err
)

echo [start] backend -> http://127.0.0.1:8000 (docs: /docs)
backend\venv\Scripts\python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000 --app-dir backend
goto :eof

:err
echo [start] failed. Check Python (^>=3.11) is installed and on PATH.
exit /b 1
