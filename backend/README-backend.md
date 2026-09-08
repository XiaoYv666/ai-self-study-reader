# Backend（M1+M2）— FastAPI + SQLite + PDF/PPT/Word 抽取 + 异步 vision 读图 + OpenAI-compatible 流式问答

接口实现《技术文档-方案.md》§5。全部示例为 **2026-08-17 实测**（macOS, Python 3.12；模型名为示例占位）。

## 启动

支持 macOS / Linux / Windows。首次使用先在 backend/ 下创建 venv 并安装依赖（三平台通用）：

```bash
cd backend
python -m venv venv          # macOS/Linux 也可用 python3
venv/bin/pip install -r requirements.txt          # macOS / Linux
venv\Scripts\python -m pip install -r requirements.txt   # Windows
# 国内网络较慢可追加：-i https://pypi.tuna.tsinghua.edu.cn/simple
```

### macOS / Linux

```bash
# 方式一：项目根 make
make backend        # 或 make dev（前端就绪后一键起前后端）

# 方式二：项目根 ./dev.sh

# 方式三：手动 uvicorn（env -u PYTHONPATH 防宿主 PYTHONPATH 指向别的 venv 串包，无此变量时可省略）
env -u PYTHONPATH backend/venv/bin/uvicorn main:app --reload \
  --host 127.0.0.1 --port 8000 --app-dir backend
```

### Windows

```bat
:: 方式一：项目根 start.bat（首次运行自动建 venv、装依赖并启动）
start.bat

:: 方式二：手动 uvicorn（如宿主设置了 PYTHONPATH 指向别的环境，先 set PYTHONPATH= 清空）
backend\venv\Scripts\python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000 --app-dir backend
```

启动后：API <http://127.0.0.1:8000>，交互式文档 <http://127.0.0.1:8000/docs>。

自检：`curl -s localhost:8000/api/health` → `{"ok":true,"providers":{"chat":{...,"ready":true}}}`

## 配置

读项目根 `.env`（不进 git）：`CHAT_BASE_URL` / `CHAT_API_KEY` / `CHAT_MODEL`，以及 `VISION_BASE_URL` / `VISION_API_KEY` / `VISION_MODEL`。
providers.py 按能力角色路由（chat / vision / image_gen），vision 一律走 `get_provider("vision")`。

## 测试数据

```bash
make seed-pdf   # 生成 data/test_sample.pdf：8 页，P4/5/7 英文、P6 中英混排、P8 无文本层
```

## 接口实测（curl 摘录）

### 1. 科目 CRUD

```bash
$ curl -s -X POST localhost:8000/api/subjects -H 'Content-Type: application/json' -d '{"name":"高等数学"}'
{"subject_id":1}

$ curl -s localhost:8000/api/subjects
[{"id":1,"name":"高等数学","sort_order":0,"doc_count":1,"created_at":"2026-08-17 13:37:10"}]

# PATCH：拖拽排序 / 改名
$ curl -s -X PATCH localhost:8000/api/subjects/1 -H 'Content-Type: application/json' -d '{"sort_order":2}'
{"ok":true}

# DELETE 只删空科目：非空返回 409 + 统计
$ curl -s -X DELETE localhost:8000/api/subjects/1
{"detail":{"msg":"科目非空，先移走内容","doc_count":1,"folder_count":2}}   # HTTP 409
```

### 2. 文件夹（≤2 层校验）

```bash
$ curl -s -X POST localhost:8000/api/folders -H 'Content-Type: application/json' \
    -d '{"subject_id":1,"parent_id":null,"name":"第一章"}'
{"folder_id":1}

$ curl -s -X POST localhost:8000/api/folders -H 'Content-Type: application/json' \
    -d '{"subject_id":1,"parent_id":1,"name":"1.1 极限"}'
{"folder_id":2}

# 第三层 → 400
$ curl -s -X POST localhost:8000/api/folders -H 'Content-Type: application/json' \
    -d '{"subject_id":1,"parent_id":2,"name":"第三层"}'
{"detail":"文件夹最多嵌套 2 层"}   # HTTP 400

# DELETE 只删空文件夹
$ curl -s -X DELETE localhost:8000/api/folders/1
{"detail":{"msg":"文件夹非空，先移走内容","subfolder_count":1,"doc_count":1}}   # HTTP 409
```

### 3. 树 & 课件移动

```bash
$ curl -s "localhost:8000/api/tree?subject_id=1"
{"subject_id":1,"folders":[...嵌套树...],"documents":[...科目根级课件...]}

# 拖入文件夹 / 跨科目移动（后端自动清空 folder_id）/ 排序 / 重命名
$ curl -s -X PATCH localhost:8000/api/documents/1 -H 'Content-Type: application/json' -d '{"folder_id":1}'
{"ok":true}
$ curl -s -X PATCH localhost:8000/api/documents/1 -H 'Content-Type: application/json' -d '{"subject_id":2}'
{"ok":true}    # doc 1 → subject 2，folder_id 置 NULL
```

### 4. 上传（multipart，PDF ≤100MB）

```bash
$ curl -s -X POST localhost:8000/api/upload -F subject_id=1 -F file=@data/test_sample.pdf
{"doc_id":1,"page_count":8,"en_pages":3,"empty_pages":[8]}

# 非法格式 → 415
$ curl -s -X POST localhost:8000/api/upload -F subject_id=1 -F file=@x.txt
{"detail":"M1 仅支持 PDF 上传，收到 .txt（pptx/docx M2 接入）"}   # HTTP 415

# 传到指定文件夹：-F folder_id=1
```

入库的逐页数据（P4/5/7 英文页被正确标记；P8 无文本层；宽高 pt 存库）：

```
page_no lang  w     h     text长度
1       zh    595.0 842.0 25
2       zh    595.0 842.0 289
4       en    595.0 842.0 844   ← 英文页
5       en    595.0 842.0 857
6       zh    595.0 842.0 224   ← 中英混排偏中文
7       en    595.0 842.0 544
8       zh    595.0 842.0 0     ← 图片页（empty）
```

### 5. PDF 流

```bash
$ curl -sI localhost:8000/api/docs/1
content-type: application/pdf
content-disposition: inline; filename="test_sample.pdf"
```

### 6. Chat（SSE 流式，调用用户配置的 chat 模型）

```bash
$ curl -sN -X POST localhost:8000/api/chat -H 'Content-Type: application/json' -d \
  '{"doc_id":1,"pages":[2,3],"question":"这两页讲的极限，请用一句话概括连续的定义","history":[]}'
```

前几个分片（真实返回）：

```
event: delta
data: {"text": "##"}

event: delta
data: {"text": " "}

event: delta
data: {"text": "前"}

event: delta
data: {"text": "置"}

event: delta
data: {"text": "回顾"}
...
event: done
data: {"usage": {"prompt_tokens": 787, "completion_tokens": 1253, "total_tokens": 2040}}
```

勾选英文页（如 P7）时自动追加双语指令，实测返回：标题中英双语、前置回顾知识点中英对照表、
解题思路全中文 + LaTeX 公式、页码引用（见 P7）、分步编号——符合 PRD 4.4 双语规则。

多轮：`history` 由前端维护（建议截最近 10 轮），后端透传拼在页面文本之前。
错误以 `event: error` 返回；页码不存在 → HTTP 404。

### 7. 对话存档 / 回看（M3 前端接入，M1 已可用）

```bash
$ curl -s -X POST localhost:8000/api/conversations -H 'Content-Type: application/json' \
    -d '{"doc_id":1,"pages":[2,3],"messages":[{"role":"user","content":"Q1"}]}'
{"conv_id":1}

$ curl -s "localhost:8000/api/conversations?doc_id=1&page=7"     # 按页回看
[{"id":2,"doc_id":1,"pages":[7],"messages":[...],"created_at":"..."}]
```

## 模块结构

| 文件 | 职责 |
|---|---|
| `main.py` | FastAPI 入口 + 全部路由（含 SSE chat） |
| `db.py` | SQLite 建表（启动自动）+ 连接管理 |
| `extract.py` | PyMuPDF 逐页抽取：文本/宽高(pt)/语言检测（`detect_lang()` 独立函数，阈值可调） |
| `prompts.py` | 系统提示词 v1（§6 原样）+ 双语指令（任一勾选页 lang='en' 触发）+ 页面注入格式 |
| `llm.py` | OpenAI 兼容流式调用封装（stream_chat）+ 非流式 chat_complete（绘图 JSON 转换用） |
| `plotgen.py` | P2.1 精确绘图：`[PLOT: {...}]` 指令解析 → chat 转 kind 专属 JSON（expr AST 白名单安全求值）→ matplotlib（函数/波形/真值表）/ schemdraw（电路/逻辑门）渲染 PNG；失败抛 PlotError 由上游降级到用户配置的 image_gen 模型 |
| `providers.py` | 能力角色路由：chat / vision（M2 实装）/ image_gen（P2 扩展位），支持 extra_headers |
| `convert.py` | LibreOffice headless 转换封装：soffice 跨平台探测（macOS/Linux 默认安装位 + Windows Program Files → PATH）、--convert-to pdf、超时与失败原因透传（M2） |
| `vision.py` | 异步读图管线：上传入队、串行 worker、页级并发、失败重试、重启恢复、进度落库（M2+） |
| `imagegen.py` | P2 画图题出图（OpenAI-compatible image model，例如 image-model / banana）：OpenAI images API 兼容 + b64/url 兜底 + httpx2 直连 |
| `make_test_pdf.py` | 生成中英混排测试 PDF |

## 接口实测（curl 摘录，M2 新增）

### 上传 pptx（M2，LibreOffice 转 PDF）

```bash
$ curl -s -X POST localhost:8000/api/upload -F subject_id=1 -F file=@data/test_sample.pptx
{"doc_id":7,"page_count":4,"en_pages":1,"empty_pages":[4],"vision_pages":[]}   # 2026-08-18 实测
```

页数/页码与 slides 一一对应（4 页，720×405pt PPT 横版）。上传时对空文本页自动 vision 回填；OpenAI-compatible 抖动失败的页留在 `empty_pages`，可用 reread 重试。另支持 .docx/.ppt/.doc。

### 异步 vision 读图管线（M2+）

上传时发现空文本页后只入库并返回，不在 `/api/upload` 同步等待读图；后台 worker 以「同一时刻 1 份课件、课件内 3 页并发」串行处理，逐页写回 `document_pages.text/lang/source='vision'`，并更新 `documents` 上的进度列：

- `vision_status`: `pending` / `running` / `done` / `partial`
- `vision_done` / `vision_total`
- `failed_pages`: JSON 数组

单页失败会自动等待后重试 1 次；仍失败则保留空文本并进入 `failed_pages`。服务重启时 startup 会扫描 `pending/running` 文档重新入队，恢复逻辑幂等跳过已有文本页。

进度可走页面接口一起拿：

```bash
$ curl -s localhost:8000/api/docs/7/pages
{"doc_id":7,"vision":{"status":"running","done":1,"total":4,"failed_pages":[]},"pages":[{"page_no":1,"width":720.0,"height":405.0,"source":"text","has_text":true},...]}
```

也可走轻量接口：

```bash
$ curl -s localhost:8000/api/docs/7/vision/status
{"doc_id":7,"vision":{"status":"done","done":1,"total":1,"failed_pages":[]}}
```

### vision 手动重读

```bash
$ curl -s -X POST localhost:8000/api/docs/7/vision/reread -H 'Content-Type: application/json' -d '{"pages":[4]}'
{"vision_pages":[4],"empty_pages":[]}   # 2026-08-18 实测：第 4 页识图回填成功
```

## 已知边界（按计划不在 M1/M2）

- CORS 目前仅放行 Vite 5173 端口
- 视觉 provider 可能因限流或网络波动返回 408/503（后台自动重试 1 次，仍失败进 `failed_pages`，可点 failed 页或用 reread 手动重试）

## 图形按需识读（chat 图形盲修复）

文字型课件页里嵌入的电路图/波形图等，chat 模型原本看不到。现机制：

- 问题命中图形关键词（`图|原理图|电路|波形|曲线|示意|figure|diagram|circuit|waveform|graph|plot`，大小写不敏感）且勾选页 `fig_desc IS NULL` 时，chat 前自动对该页跑 vision 图形描述（页级并发 3、单页 45s、总超时 90s）
- 关键词未命中时走**物理检测通道**（2026-08-19 重构，替代原 LLM 预判）：上传时 PyMuPDF `page.get_images(full=True)` 检测页内嵌入位图（宽或高 ≥100px 算内容图）写入 `document_pages.has_image`（1=有/0=无/NULL=未检测）；chat 时勾选页 `has_image=1` 且 `fig_desc IS NULL` 直接识图——「页面是否有图」是 PDF 结构事实，不再让 LLM 看文字层猜（文字层不含图内信息，曾致 doc15 P4 电路参数漏识别）。存量页由启动时后台补跑回填（日志 `has_image_backfill done/total`），`has_image=0` 或已缓存（含空串「无图形」）不识
- 上传完成后会后台自动预跑该课件 `has_image=1 AND fig_desc IS NULL` 的图形描述，复用 vision 全局单课件串行队列（避免与整页无文本转录同时占用同一 provider），课件内页级并发 3；单课件最多预跑前 40 页，超过时日志 `fig_prerun_limit` 说明截断。失败页不重试且不写空串，保持 `NULL`，后续 chat 仍可按需兜底。
- 服务启动时，`has_image_backfill` 完成后会扫描所有仍存在 `has_image=1 AND fig_desc IS NULL` 的课件并入队预跑，覆盖重启恢复与存量补漏；同一 doc 在 fig 预跑队列中自动去重。
- 手动触发：`POST /api/docs/{id}/fig/prerun`，默认只跑该课件未缓存的含图页；body 可传 `{"force": true}` 先清空该 doc 全部 `fig_desc` 后重识。返回包含 `queued`、`pending_pages`、`will_run_pages`、`limit`。
- 描述写入 `document_pages.fig_desc` 永久缓存；重复问直接命中，不再调用 vision
- 注入格式：页面文本后追加 `【图中内容（AI 识图）】` 块；超时/失败静默跳过不阻塞回答（`fig_desc` 保持 NULL 下次重试）
- 日志关键字：`fig_prerun`（后台预跑汇总，含 pages/saved/failed/elapsed）、`fig_prerun_limit`（40 页上限截断）、`chat_fig_desc`（兜底触发与落库，`source=keyword|has_image`）、`fig_desc_total_timeout`（总超时放弃）
- 图片型课件（整页 vision 转录）的 prompt 已同步升级：转录文本后附图形结构描述段，上传时一次过全齐
