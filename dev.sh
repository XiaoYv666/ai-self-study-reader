#!/usr/bin/env bash
# 一键启动开发环境（macOS / Linux；Windows 请用 start.bat）
# 首次使用先建 venv：python3 -m venv backend/venv && backend/venv/bin/pip install -r backend/requirements.txt
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p data/originals data/pdfs

# 后端：uvicorn 热重载（env -u PYTHONPATH 防宿主 PYTHONPATH 指向别的 venv 串包）
echo "[dev] backend → http://127.0.0.1:8000 (docs: /docs)"
env -u PYTHONPATH backend/venv/bin/uvicorn main:app --reload --host 127.0.0.1 --port 8000 --app-dir backend &

BACKEND_PID=$!
trap 'kill $BACKEND_PID 2>/dev/null || true' EXIT

# TODO: frontend（另一 agent 实现中）：cd frontend && npm run dev &

wait $BACKEND_PID
