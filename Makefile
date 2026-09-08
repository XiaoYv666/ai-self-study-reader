# AI 自学阅读器 · 开发启动（技术文档 §10）—— macOS / Linux（Windows 请用 start.bat）
# make dev      一键起后端（前端就绪后追加 frontend 目标）
# make backend  仅起后端（uvicorn 热重载，端口 8000）
# make seed-pdf 生成 8 页中英混排测试 PDF 到 data/test_sample.pdf
#
# 依赖：python3 -m venv backend/venv && backend/venv/bin/pip install -r backend/requirements.txt
# env -u PYTHONPATH：通用防串包（宿主 PYTHONPATH 指向别的 venv 时），无此变量时无副作用

PY = backend/venv/bin/python
UVICORN = backend/venv/bin/uvicorn

.PHONY: dev backend seed-pdf clean

dev: backend
	# TODO: frontend（另一 agent 实现中）就绪后在此并行启动：cd frontend && npm run dev
	@echo "backend running on http://localhost:8000"

backend:
	@mkdir -p data/originals data/pdfs
	cd backend && env -u PYTHONPATH ../backend/venv/bin/uvicorn main:app --reload --host 127.0.0.1 --port 8000

seed-pdf:
	env -u PYTHONPATH $(PY) make_test_pdf.py

clean:
	rm -rf data/app.db data/originals/* data/pdfs/*
