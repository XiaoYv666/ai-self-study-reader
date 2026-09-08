"""异步 vision 读图管线：空文本页 → PNG → vision 模型转写 → 回填 document_pages。

设计约定：
- 一律走 get_provider("vision")，不写死模型名；extra_headers（CF Access 门禁）由 sdk 带上
- PyMuPDF 渲染 PNG（150 DPI，识图够用且 base64 体积可控）
- 单课件串行队列（同一时刻只跑 1 份课件），课件内页级并发 3，单页 60s
- 上传链路只入库并 enqueue，立即返回；重启后 startup 扫 pending/running 自动恢复
- 单页失败后延迟重试 1 次，仍失败写入 failed_pages；成功页 source='vision'
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from contextlib import suppress
from typing import Awaitable, Callable

import pymupdf

import db
from extract import detect_lang
from providers import get_provider

logger = logging.getLogger("vision")

# ---- 可调参数（集中在此，调优不动业务代码） ----
RENDER_DPI = 150          # 页面渲染 DPI
PAGE_CONCURRENCY = 3      # 读图并发页数
PAGE_TIMEOUT_S = 60       # 单页读图超时
RETRY_DELAY_S = 2         # 单页失败后重试前等待（单测可传 0）
FIG_DESC_TIMEOUT_S = 45       # 单页图形识读超时（chat 按需路径）
FIG_DESC_CONCURRENCY = 3      # chat 按需识读页级并发
FIG_DESC_TOTAL_TIMEOUT_S = 90 # chat 按需识读总超时（到点放弃，已完成页保留；实测 OpenAI-compatible 单页约 30-60s，40s 首问常超时）
FIG_PRERUN_PAGE_LIMIT = 40    # 上传/启动后台 fig_desc 预跑单课件页数上限
FIG_PRERUN_TOTAL_TIMEOUT_S = 15 * 60  # 预跑路径允许排队慢跑，避免首问卡住

VISION_PROMPT = (
    "你是课件转写器。请把这张课件页图片中的内容完整转写为纯文本，用于后续的检索与问答。要求：\n"
    "1. 按阅读顺序转写标题、正文、列表、表格、图表标注、页脚等所有可见文字；\n"
    "2. 数学公式用 LaTeX 表示（行内 $...$，独立公式 $$...$$）；\n"
    "3. 保留合理的段落与换行结构，不要添加任何解释、总结或原文没有的内容；\n"
    "4. 若页面含图形（电路图/波形图/函数曲线/几何图/表格图/流程图/示意图等），"
    "在文字转写结束后另起一段，以「【图中内容】」开头描述图形结构：电路图写元件清单+连接拓扑+标注值，"
    "波形图写信号名/形状/周期/电平，函数曲线写变量与曲线形状，其余图形写关键构成与标注；"
    "中文描述，关键术语附英文对照；\n"
    "5. 图片确实无文字且无图形时，输出一行描述页面内容的简短文字（如「图：函数图像示意图」）；\n"
    "6. 直接输出转写结果，不要输出任何前缀。"
)

# 图形识读专用 prompt（chat 按需 describe_page_figures 用，与文字转写分离）
FIGURE_DESC_PROMPT = (
    "你是工程课件读图助手。这张课件页里可能嵌有图形，请专门描述其中的图形内容，"
    "输出一段中文描述（关键术语附英文对照），不要转写正文文字。要求：\n"
    "1. 电路原理图 (circuit diagram)：列出元件清单（类型/编号/参数值），说明连接拓扑"
    "（输入/输出/电源/地、各元件之间如何相连、关键节点）；\n"
    "2. 波形图 (waveform/timing diagram)：写出各信号名、形状（方波/正弦/三角等）、"
    "周期/相位关系、高低电平与跳变沿；\n"
    "3. 函数曲线 (function plot)：写出坐标轴变量、曲线条数与形状、交点/极值等关键点；\n"
    "4. 几何图 (geometry)：写出图形构成与标注的边、角、符号；\n"
    "5. 表格图 (table)：写出表头与行列结构概要及关键单元格数据；\n"
    "6. 流程图 (flowchart)：写出节点与分支走向；\n"
    "7. 其他示意图照实描述结构与标注；\n"
    "8. 图中出现的数值/标注（元件参数值、坐标刻度与关键点、角度、长度、表格数据等）"
    "必须原样提取，不得省略或改写；\n"
    "9. 一页有多张图时逐张描述，并标注位置（如「左图」「下图」）；\n"
    "10. 页面上确实没有任何图形时，只输出三个字：无图形；\n"
    "11. 直接输出描述，不要输出任何前缀或解释。"
)

ReadPageFn = Callable[[bytes], Awaitable[str]]
RenderPageFn = Callable[..., bytes]

_queue: asyncio.Queue[tuple[str, int]] | None = None
_worker_task: asyncio.Task | None = None
_background_tasks: set[asyncio.Task] = set()
_queued_doc_ids: set[int] = set()
_queued_fig_doc_ids: set[int] = set()
_running_doc_id: int | None = None
_running_job: tuple[str, int] | None = None

JOB_TRANSCRIBE = "transcribe"
JOB_FIG_PRERUN = "fig_prerun"


def render_page_png(pdf_path: str, page_no: int, dpi: int = RENDER_DPI) -> bytes:
    """渲染 1-based 页码为 PNG bytes（150 DPI）。"""
    with pymupymupdf_open(pdf_path) as doc:
        page = doc[page_no - 1]  # 0-based
        pix = page.get_pixmap(dpi=dpi)
        return pix.tobytes("png")


def pymupymupdf_open(pdf_path: str):
    """小包装便于单测 monkeypatch，同时保持默认 PyMuPDF 行为。"""
    return pymupdf.open(pdf_path)


def build_vision_messages(png_b64: str) -> list[dict]:
    """OpenAI 兼容 chat.completions 消息拼装（独立成函数，单测覆盖拼装逻辑）。"""
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": VISION_PROMPT},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{png_b64}"},
                },
            ],
        }
    ]


async def read_page_text(png: bytes) -> str:
    """调 vision provider 读单页图，返回转写文本。异常向上抛（调用方决定降级）。"""
    import httpx2 as httpx  # 局部导入，与 llm.py 同风格可测
    from openai import AsyncOpenAI  # 局部导入，与 llm.py 同风格可测

    cfg = get_provider("vision")
    png_b64 = base64.b64encode(png).decode("ascii")
    messages = build_vision_messages(png_b64)
    client = AsyncOpenAI(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        default_headers=cfg.extra_headers or None,
        timeout=PAGE_TIMEOUT_S,
        max_retries=0,
        # trust_env=False：不读系统/环境代理（macOS scutil 代理可能指向死端口）
        http_client=httpx.AsyncClient(trust_env=False, timeout=PAGE_TIMEOUT_S),
    )
    resp = await client.chat.completions.create(
        model=cfg.model,
        messages=messages,
        temperature=0,
    )
    content = resp.choices[0].message.content or ""
    return content.strip()


# ---------- 图形按需识读（chat 触发，结果缓存 document_pages.fig_desc） ----------

def build_figure_desc_messages(png_b64: str) -> list[dict]:
    """图形识读消息拼装（独立成函数，单测覆盖拼装逻辑）。"""
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": FIGURE_DESC_PROMPT},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{png_b64}"},
                },
            ],
        }
    ]


async def describe_page_figures(png: bytes) -> str:
    """调 vision provider 描述单页图形，返回中文描述；无图形返回空串。

    异常向上抛（调用方决定降级）；复用 read_page_text 的客户端构建方式。
    """
    import httpx2 as httpx  # 局部导入，与 llm.py 同风格可测
    from openai import AsyncOpenAI  # 局部导入，与 llm.py 同风格可测

    cfg = get_provider("vision")
    png_b64 = base64.b64encode(png).decode("ascii")
    messages = build_figure_desc_messages(png_b64)
    client = AsyncOpenAI(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        default_headers=cfg.extra_headers or None,
        timeout=FIG_DESC_TIMEOUT_S,
        max_retries=0,
        # trust_env=False：不读系统/环境代理（macOS scutil 代理可能指向死端口）
        http_client=httpx.AsyncClient(trust_env=False, timeout=FIG_DESC_TIMEOUT_S),
    )
    resp = await client.chat.completions.create(
        model=cfg.model,
        messages=messages,
        temperature=0,
    )
    content = (resp.choices[0].message.content or "").strip()
    # 「无图形」约定 → 归一化为空串缓存（下次不再跑，注入时也自然跳过）
    if content in ("无图形", "无图形。"):
        return ""
    return content


def save_fig_desc(doc_id: int, page_no: int, fig_desc: str) -> None:
    """识读结果写回缓存列（空串也是有效缓存：表示该页无图形）。"""
    conn = db.get_conn()
    conn.execute(
        "UPDATE document_pages SET fig_desc=? WHERE doc_id=? AND page_no=?",
        (fig_desc, doc_id, page_no),
    )
    conn.commit()


async def describe_doc_figures(
    doc_id: int,
    pdf_path: str,
    page_nos: list[int],
    concurrency: int = FIG_DESC_CONCURRENCY,
    describe_page=None,
    render_page: RenderPageFn = render_page_png,
    total_timeout_s: float = FIG_DESC_TOTAL_TIMEOUT_S,
) -> list[int]:
    """对一批页并发识读图形并缓存 fig_desc；返回成功页码列表。

    - 单页失败/超时静默跳过（fig_desc 保持 NULL，下次可再试）
    - 总超时到点放弃未完成页；已完成的页已写库保留
    - describe_page 依赖注入：单测换 mock
    """
    if not page_nos:
        return []
    if describe_page is None:
        describe_page = describe_page_figures

    sem = asyncio.Semaphore(concurrency)

    async def guarded(page_no: int) -> tuple[int, str | None]:
        async with sem:
            try:
                png = await asyncio.to_thread(render_page, pdf_path, page_no)
                desc = await asyncio.wait_for(describe_page(png), timeout=FIG_DESC_TIMEOUT_S)
                desc = (desc or "").strip()
                # 「无图形」约定 → 归一化为空串缓存（该页已识读、无图，下次不再跑）
                if desc in ("无图形", "无图形。"):
                    desc = ""
                return page_no, desc
            except Exception as e:  # noqa: BLE001 —— 单页失败不影响 chat
                logger.warning("fig_desc_failed doc=%s page=%s: %s", doc_id, page_no, e)
                return page_no, None

    tasks = [asyncio.create_task(guarded(p)) for p in page_nos]
    done, pending = await asyncio.wait(
        tasks, timeout=total_timeout_s, return_when=asyncio.ALL_COMPLETED
    )
    for t in pending:  # 总超时未完成的页放弃（fig_desc 保持 NULL）
        t.cancel()
        logger.warning("fig_desc_total_timeout doc=%s, 放弃 %s 页", doc_id, len(pending))
    saved: list[int] = []
    for t in done:
        page_no, desc = t.result()
        if desc is not None:
            save_fig_desc(doc_id, page_no, desc)
            saved.append(page_no)
    return sorted(saved)


def _loads_failed(raw: str | None) -> list[int]:
    try:
        data = json.loads(raw or "[]")
        return sorted({int(x) for x in data})
    except Exception:  # noqa: BLE001
        return []


def _save_progress(doc_id: int, *, status: str | None = None, done: int | None = None,
                   total: int | None = None, failed_pages: list[int] | None = None) -> None:
    """持久化 vision 进度；每次调用独立 commit，便于轮询与崩溃恢复。"""
    fields: list[str] = []
    params: list[object] = []
    if status is not None:
        fields.append("vision_status=?")
        params.append(status)
    if done is not None:
        fields.append("vision_done=?")
        params.append(done)
    if total is not None:
        fields.append("vision_total=?")
        params.append(total)
    if failed_pages is not None:
        fields.append("failed_pages=?")
        params.append(json.dumps(sorted(set(failed_pages)), ensure_ascii=False))
    if not fields:
        return
    conn = db.get_conn()
    conn.execute(f"UPDATE documents SET {', '.join(fields)} WHERE id=?", (*params, doc_id))
    conn.commit()


def get_doc_vision_status(doc_id: int) -> dict:
    """返回文档级 vision 进度对象。"""
    row = db.get_conn().execute(
        "SELECT vision_status, vision_done, vision_total, failed_pages FROM documents WHERE id=?",
        (doc_id,),
    ).fetchone()
    if not row:
        return {"status": "done", "done": 0, "total": 0, "failed_pages": []}
    return {
        "status": row["vision_status"],
        "done": int(row["vision_done"] or 0),
        "total": int(row["vision_total"] or 0),
        "failed_pages": _loads_failed(row["failed_pages"]),
    }


def init_doc_vision_job(doc_id: int, total: int) -> None:
    """上传后初始化文档 vision 状态。total=0 时直接 done。"""
    if total > 0:
        _save_progress(doc_id, status="pending", done=0, total=total, failed_pages=[])
    else:
        _save_progress(doc_id, status="done", done=0, total=0, failed_pages=[])


async def _read_one_with_retry(page_no: int, pdf_path: str, read_page: ReadPageFn,
                               render_page: RenderPageFn, retry_delay_s: float) -> tuple[int, str | None]:
    for attempt in (1, 2):
        try:
            png = render_page(pdf_path, page_no)
            text = await asyncio.wait_for(read_page(png), timeout=PAGE_TIMEOUT_S)
            text = (text or "").strip()
            if text:
                return page_no, text
            raise RuntimeError("vision 输出为空")
        except Exception as e:  # noqa: BLE001 —— 单页失败不影响整体
            logger.warning("vision_read_failed page=%s attempt=%s: %s", page_no, attempt, e)
            if attempt == 1:
                await asyncio.sleep(retry_delay_s)
    return page_no, None


async def transcribe_pages(
    doc_id: int,
    pdf_path: str,
    page_nos: list[int],
    page_limit: int | None = None,  # 兼容旧调用；异步主链路不再限页
    concurrency: int = PAGE_CONCURRENCY,
    read_page: ReadPageFn = read_page_text,       # 依赖注入：单测换 mock
    render_page: RenderPageFn = render_page_png,  # 依赖注入：单测换 mock
    retry_delay_s: float = RETRY_DELAY_S,
    update_progress: bool = False,
) -> tuple[list[int], list[int]]:
    """读图回填一批空文本页。

    - 不再按总页数截断；page_limit 仅为旧签名兼容，忽略
    - 每页失败后自动重试 1 次；仍失败保持空文本，返回 failed_pages
    - update_progress=True 时逐页更新 documents.vision_done/failed_pages/status
    """
    del page_limit
    # 幂等：跳过已经有文本的页，避免重启恢复/重复入队覆盖用户数据。
    conn = db.get_conn()
    if not page_nos:
        return [], []
    placeholders = ",".join("?" for _ in page_nos)
    rows = conn.execute(
        f"SELECT page_no, text FROM document_pages WHERE doc_id=? AND page_no IN ({placeholders}) ORDER BY page_no",
        (doc_id, *page_nos),
    ).fetchall()
    targets = [r["page_no"] for r in rows if not r["text"]]
    already_done = len(rows) - len(targets)
    if update_progress and already_done:
        status = get_doc_vision_status(doc_id)
        _save_progress(doc_id, done=min(status["total"], status["done"] + already_done))
    if not targets:
        return [], []

    sem = asyncio.Semaphore(concurrency)

    async def guarded(page_no: int) -> tuple[int, str | None]:
        async with sem:
            return await _read_one_with_retry(page_no, pdf_path, read_page, render_page, retry_delay_s)

    tasks = [asyncio.create_task(guarded(p)) for p in targets]
    vision_pages: list[int] = []
    failed_pages: list[int] = []

    for fut in asyncio.as_completed(tasks):
        page_no, text = await fut
        if text:
            conn.execute(
                "UPDATE document_pages SET text=?, lang=?, source='vision' WHERE doc_id=? AND page_no=?",
                (text, detect_lang(text), doc_id, page_no),
            )
            conn.commit()
            vision_pages.append(page_no)
        else:
            failed_pages.append(page_no)
        if update_progress:
            status = get_doc_vision_status(doc_id)
            merged_failed = sorted(set(status["failed_pages"]) | ({page_no} if not text else set()))
            _save_progress(doc_id, done=min(status["total"], status["done"] + 1), failed_pages=merged_failed)

    return sorted(vision_pages), sorted(failed_pages)


async def run_document_job(doc_id: int, *, read_page: ReadPageFn = read_page_text,
                           render_page: RenderPageFn = render_page_png,
                           retry_delay_s: float = RETRY_DELAY_S) -> None:
    """执行单个文档的 vision 读图；可由队列 worker 或单测/恢复函数直接调用。"""
    conn = db.get_conn()
    doc = conn.execute("SELECT id, pdf_path, vision_total FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        return
    empty_rows = conn.execute(
        "SELECT page_no FROM document_pages WHERE doc_id=? AND text='' ORDER BY page_no",
        (doc_id,),
    ).fetchall()
    targets = [r["page_no"] for r in empty_rows]
    if not targets:
        _save_progress(doc_id, status="done", done=int(doc["vision_total"] or 0), failed_pages=[])
        return

    status = get_doc_vision_status(doc_id)
    total = status["total"] or len(targets)
    done_so_far = max(0, total - len(targets))
    _save_progress(doc_id, status="running", total=total, done=done_so_far, failed_pages=[])
    _, failed = await transcribe_pages(
        doc_id,
        doc["pdf_path"],
        targets,
        concurrency=PAGE_CONCURRENCY,
        read_page=read_page,
        render_page=render_page,
        retry_delay_s=retry_delay_s,
        update_progress=True,
    )
    final_status = "partial" if failed else "done"
    _save_progress(doc_id, status=final_status, done=total, failed_pages=failed)


def _fig_prerun_targets(doc_id: int, *, limit: int = FIG_PRERUN_PAGE_LIMIT) -> tuple[str | None, list[int], int]:
    """返回 (pdf_path, page_nos, total_pending)：只取 has_image=1 且 fig_desc IS NULL 的页。"""
    conn = db.get_conn()
    doc = conn.execute("SELECT id, pdf_path FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        return None, [], 0
    rows = conn.execute(
        """SELECT page_no FROM document_pages
           WHERE doc_id=? AND has_image=1 AND fig_desc IS NULL
           ORDER BY page_no""",
        (doc_id,),
    ).fetchall()
    page_nos = [int(r["page_no"]) for r in rows]
    return str(doc["pdf_path"]), page_nos[:limit], len(page_nos)


async def run_fig_prerun_job(
    doc_id: int,
    *,
    describe_page=None,
    render_page: RenderPageFn = render_page_png,
) -> None:
    """后台预跑单课件所有 has_image=1 且 fig_desc 未缓存页（最多 40 页）。

    失败页不重试、不落空串，保持 NULL，chat 按需路径仍可兜底。
    """
    pdf_path, targets, total_pending = _fig_prerun_targets(doc_id)
    if not pdf_path:
        return
    if not targets:
        logger.info("fig_prerun doc=%s pages=[] saved=[] failed=0 elapsed=0.0s", doc_id)
        return
    if total_pending > len(targets):
        logger.info(
            "fig_prerun_limit doc=%s pending=%s limit=%s, only first %s pages queued",
            doc_id, total_pending, FIG_PRERUN_PAGE_LIMIT, len(targets),
        )

    t0 = time.monotonic()
    saved = await describe_doc_figures(
        doc_id,
        pdf_path,
        targets,
        concurrency=FIG_DESC_CONCURRENCY,
        describe_page=describe_page,
        render_page=render_page,
        total_timeout_s=FIG_PRERUN_TOTAL_TIMEOUT_S,
    )
    failed = sorted(set(targets) - set(saved))
    logger.info(
        "fig_prerun doc=%s pages=%s saved=%s failed=%s elapsed=%.1fs",
        doc_id, targets, saved, len(failed), time.monotonic() - t0,
    )


def enqueue_vision_doc(doc_id: int) -> bool:
    """将文档加入后台 vision 队列；返回是否新入队。"""
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    if _running_job == (JOB_TRANSCRIBE, doc_id) or doc_id in _queued_doc_ids:
        return False
    _queued_doc_ids.add(doc_id)
    _queue.put_nowait((JOB_TRANSCRIBE, doc_id))
    return True


def enqueue_fig_prerun(doc_id: int) -> bool:
    """将文档加入后台 fig_desc 预跑队列；同一 doc 已排队/运行时不重复入队。"""
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    if _running_job == (JOB_FIG_PRERUN, doc_id) or doc_id in _queued_fig_doc_ids:
        return False
    _queued_fig_doc_ids.add(doc_id)
    _queue.put_nowait((JOB_FIG_PRERUN, doc_id))
    return True


async def enqueue_fig_prerun_async(doc_id: int) -> bool:
    """上传完成后 create_task 调用的小包装；实际入队仍是同步、无阻塞。"""
    return enqueue_fig_prerun(doc_id)


async def _worker_loop() -> None:
    global _running_doc_id, _running_job
    assert _queue is not None
    while True:
        job = await _queue.get()
        # 兼容单测/旧进程里可能残留的 int 队列项。
        if isinstance(job, int):  # type: ignore[unreachable]
            kind, doc_id = JOB_TRANSCRIBE, job
        else:
            kind, doc_id = job
        if kind == JOB_FIG_PRERUN:
            _queued_fig_doc_ids.discard(doc_id)
        else:
            _queued_doc_ids.discard(doc_id)
        _running_job = (kind, doc_id)
        _running_doc_id = doc_id if kind == JOB_TRANSCRIBE else None
        try:
            if kind == JOB_FIG_PRERUN:
                await run_fig_prerun_job(doc_id)
            else:
                await run_document_job(doc_id)
        except Exception as e:  # noqa: BLE001 —— worker 常驻，单文档失败不杀队列
            logger.exception("vision_job_failed kind=%s doc=%s: %s", kind, doc_id, e)
            if kind == JOB_TRANSCRIBE:
                status = get_doc_vision_status(doc_id)
                _save_progress(doc_id, status="partial", failed_pages=status["failed_pages"])
        finally:
            _running_job = None
            _running_doc_id = None
            _queue.task_done()


def start_background_worker() -> None:
    """启动全局串行 worker，并保留 task 引用防 GC。"""
    global _queue, _worker_task
    if _queue is None:
        _queue = asyncio.Queue()
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker_loop(), name="vision-worker")
        _background_tasks.add(_worker_task)
        _worker_task.add_done_callback(_background_tasks.discard)


def recover_pending_jobs() -> list[int]:
    """服务启动恢复：pending/running 文档重新入队（幂等跳过已有文本页）。"""
    rows = db.get_conn().execute(
        "SELECT id FROM documents WHERE vision_status IN ('pending','running') ORDER BY id"
    ).fetchall()
    recovered: list[int] = []
    for row in rows:
        doc_id = int(row["id"])
        if enqueue_vision_doc(doc_id):
            recovered.append(doc_id)
    return recovered


def recover_pending_fig_preruns() -> list[int]:
    """服务启动/has_image 回填后扫描需要 fig_desc 预跑的课件并入队。"""
    rows = db.get_conn().execute(
        """SELECT DISTINCT d.id
           FROM documents d
           JOIN document_pages dp ON dp.doc_id = d.id
           WHERE dp.has_image=1 AND dp.fig_desc IS NULL
           ORDER BY d.id"""
    ).fetchall()
    recovered: list[int] = []
    for row in rows:
        doc_id = int(row["id"])
        if enqueue_fig_prerun(doc_id):
            recovered.append(doc_id)
    return recovered


async def stop_background_worker() -> None:
    """测试/服务退出时可调用；生产退出容忍中断，重启恢复。"""
    global _worker_task
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await _worker_task
    _worker_task = None
