# AI 自学阅读器（课件阅读 + AI 助教）

面向大学生自学课件（PPT/PDF/Word）的本地阅读器：左侧科目树管理课件，中间连续滚动预览 PDF，点击页面即可「勾选」，右侧 AI 助教基于勾选页内容做系统性讲解（前置回顾 → 分步解题 → 中英双语 → 举一反三），支持对话存档与按页码回看。

**状态：M1–M6 全部完成并开源（2026-09-08）**。全链路：科目管理 → 课件上传（PDF/PPT/Word）→ 连续滚动预览 + 勾页 → 逐页索引 → AI 流式双语问答（含读图、精确绘图、生图降级）→ 对话存档回看。

---

## 快速开始

前置：macOS / Linux / Windows + Python 3.11/3.12 + Node ≥ 20。上传 PPT/Word 课件需本机安装 [LibreOffice](https://www.libreoffice.org/)（纯 PDF 课件无需安装）。

### macOS / Linux

```bash
# 后端（8000）：首次先建 venv 并装依赖
python3 -m venv backend/venv
backend/venv/bin/pip install -r backend/requirements.txt
# 国内网络较慢可追加：-i https://pypi.tuna.tsinghua.edu.cn/simple

make backend      # 或 ./dev.sh

# 前端（5173，另开终端）
cd frontend && npm install
npm run dev
```

### Windows

```bat
:: 后端（8000）：首次运行自动建 venv 并装依赖
start.bat

:: 前端（5173，另开终端）
cd frontend
npm install
npm run dev
```

> Windows 手动等价命令：`python -m venv backend\venv` → `backend\venv\Scripts\python -m pip install -r backend\requirements.txt` → `backend\venv\Scripts\python -m uvicorn main:app --reload --port 8000 --app-dir backend`。npm 国内较慢时可用 `npm install --registry=https://registry.npmmirror.com`。

打开 http://localhost:5173 （`127.0.0.1`/`localhost` 即使用者本机；若端口被占用，Vite 会自动改用 5174 等，以终端实际打印的地址为准。Vite 已代理 /api → 8000，无需 CORS 配置）。

首次使用：

1. **配置模型与 API**（AI 问答前必须）：左栏底部「设置 · 模型与 API」→ 添加 Provider（类型 `openai_compatible`，填 Base URL + API Key）→ 添加模型并选择能力（推理 chat / 识图 vision / 生图 image_gen）→ 启用并「测试连接」。支持任意 OpenAI-compatible 服务或本地模型服务；模型名只需填写供应商文档里的实际标识，例如 `chat-model` / `vision-model` / `image-model`。弹窗内「?」有完整配置教程。也可用 `.env`（参考 `.env.example`）。
2. **建科目、传课件**：左侧「新建科目」→「上传课件」（支持 PDF/PPT/Word，PPT/Word 自动转 PDF，图片页 AI 识图补全）。
3. **开始学习**：点击课件 → 中间预览区点选若干页 → 右侧输入问题。

> 后端未启动时前端自动降级为「演示模式」（左栏显示「演示」金标），内置 mock 数据可完整体验交互；后端就绪后自动切真实数据。仓库已附带 6 份合成 mock 课件 PDF（`frontend/public/mock/`）；如需重新生成可运行 `env -u PYTHONPATH backend/venv/bin/python scripts/gen_mock_pdfs.py`。

## 用户使用流程（从零到提问）

1. `git clone` 或下载 ZIP 解压，进入项目目录
2. 按上方「快速开始」启动后端与前端（Windows 双击 `start.bat`）
3. 浏览器打开 http://localhost:5173
4. 左栏底部「设置 · 模型与 API」配置 Provider + 模型（详见上文三步）
5. 新建科目 → 上传课件（PDF 直接传；PPT/Word 需 LibreOffice）→ 勾选页面 → 提问
6. 数据全部存在本地 `data/` 目录（SQLite + 文件），删除目录即完全卸载

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | React 19 + Vite + TypeScript + Tailwind v4 + react-pdf（连续滚动/勾选） |
| 后端 | FastAPI + SQLite（WAL）+ PyMuPDF（逐页抽取）+ OpenAI-compatible 模型配置（用户自接 API） |
| 视觉 | V2 温暖纸感（米白纸纹 + 衬线标题 + 暖棕强调），色板见技术文档 §1.2 |
| 交互 | 点页勾选（多选）、页码 chip 定位回跳、聊天栏宽度可拖（300–540px 持久化）、阅读区缩放 60–200% + 横向滚动 |

## 目录结构

```
├── backend/            # FastAPI：main/db/extract/llm/providers/prompts
├── frontend/           # React 应用（src/components 三栏组件）
├── data/               # 运行时数据（app.db / originals/ / pdfs/，不进 git）
├── scripts/            # 工具脚本（如 gen_mock_pdfs.py）
├── 需求文档-PRD.md     # 做什么（v0.2）
├── 技术文档-方案.md    # 怎么做（v0.5）
├── 前端选型调研.md     # UI 技术选型依据
└── Makefile / dev.sh / start.bat   # 启动入口（POSIX / Windows）
```

## 核心接口（详见技术文档 §5）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST/GET | /api/subjects | 科目 CRUD（DELETE 只删空） |
| POST/GET/DELETE | /api/folders | 文件夹（≤2 层，只删空） |
| GET | /api/tree?subject_id= | 侧栏整棵树 |
| POST | /api/upload | 上传 PDF → 转 PDF → 逐页抽取/语言/尺寸入库 |
| GET | /api/docs/{id} | PDF 文件流（RFC 5987 中文文件名） |
| GET | /api/docs/{id}/pages | 每页宽高元数据 |
| POST | /api/chat | SSE 流式问答（delta/done/error） |
| POST/GET | /api/conversations | 对话存档 + 按页回查（M3 前端接入） |

## 开发里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | PDF 全链路（预览/勾页/索引/双语问答） | ✅ 2026-08-17 |
| M2 | PPT/Word 转换（LibreOffice）+ vision 读图兜底 | ✅ 2026-08-18（pptx/docx/ppt/doc 五格式上传 + 图片型课件异步识图：上传即返回、后台进度轮询、失败重试/重启恢复 + reread 重读接口；openai 客户端 trust_env=False 规避死系统代理） |
| M3 | 对话存档回看 + 侧栏整理（拖拽/文件夹/跨科目移动） | ✅ 2026-08-17 |
| M4 | 模型与 API 配置中心（页面添加/测试/切换 Provider 与模型） | ✅ 2026-08-23（Provider CRUD + 能力路由 chat/vision/image_gen + 优先级 + 测试连接 + 密钥加密存储） |
| M5 | 多模态讲解管线（读图识图生图） | ✅ 2026-08-30（figure 描述预跑 + [PLOT] matplotlib/schemdraw 精确绘图 + [DRAW] 生图降级链路） |
| M6 | 对话交互增强 + 数学公式协议 | ✅ 2026-09-08（流式中断/重发、abortableSSE、KaTeX 数学渲染协议、对话存档图片回看） |
| P2+ | 画图题（image_gen 模型出图） | ✅ 已随 M5/M6 落地（精确绘制优先、生图降级） |

## 环境说明（踩过的坑）

- **PYTHONPATH 串包**：若宿主环境设置了 `PYTHONPATH` 且指向其他 venv 的 site-packages，后端可能 import 到别的环境的包。POSIX 下可用 `env -u PYTHONPATH` 规避（Makefile/dev.sh 已内置）；Windows 下可在启动前 `set PYTHONPATH=` 清空
- **中文文件名 PDF**：`Content-Disposition` 头需 RFC 5987（filename + filename*），否则 Starlette latin-1 报 500
- **SSE 契约**：delta 事件载荷 `{"type":"delta","content":...}`，前后端必须一致
- **页面宽高比**：每页按 `document_pages.width/height` 渲染（A4 竖版 0.71 / PPT 横版 1.29 / 混排自适应），宽度自适应容器 + CSS aspect-ratio 驱动高度
- **依赖镜像**：国内网络较慢时，pip 可加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`，npm 可加 `--registry=https://registry.npmmirror.com`（均为可选项，不写死在脚本里）

## 文档索引

- PRD：`需求文档-PRD.md`
- 技术方案：`技术文档-方案.md`
- 后端实测：`backend/README-backend.md`
- 前端启动：`frontend/`（npm run dev）
