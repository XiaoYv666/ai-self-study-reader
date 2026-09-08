"""FastAPI 入口 + 全部路由（M1）。

接口按《技术文档-方案.md》§5：
- /api/subjects  科目 CRUD（DELETE 只删空科目，409 + 统计）
- /api/folders   文件夹 CRUD（≤2 层校验，DELETE 只删空）
- /api/tree      科目下整棵树（文件夹 + 课件）
- PATCH          subjects/documents（sort_order / name / 归属移动）
- /api/upload    multipart 上传 → originals/ → PDF 逐页抽取入库
- /api/docs/{id} PDF 文件流
- /api/chat      SSE 流式问答（delta / image / done / error；P2 画图题出图）
- /api/images    chat 生成图片静态服务（P2）
- /api/conversations 对话保存与按页回查（M1 一并落库，前端 M3 接）
"""
import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import time
from urllib.parse import quote
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import convert
import db
import vision
from extract import extract_pdf, page_has_image
from imagegen import generate_image
from llm import chat_complete, stream_chat
from plotgen import (
    PlotError,
    fallback_prompt,
    generate_plot,
    resolve_plot_caption,
    resolve_plot_instruction,
)
from prompts import build_messages

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ORIGINALS_DIR = PROJECT_ROOT / "data" / "originals"
PDFS_DIR = PROJECT_ROOT / "data" / "pdfs"
IMAGES_DIR = PROJECT_ROOT / "data" / "images"  # P2：chat 出图落盘目录

MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100MB（PRD 4.1）
ALLOWED_EXT = {".pdf", ".pptx", ".docx", ".ppt", ".doc"}  # M2：非 PDF 走 convert.py（LibreOffice）转 PDF，旧格式 .ppt/.doc 同链路支持

app = FastAPI(title="doc-reader-ai backend", version="0.1.0")

# 前端 Vite dev (5173) 直连；M1 阶段先放开本地端口
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    db.init_db()
    import providers
    migration = providers.migrate_legacy_env_config()
    ORIGINALS_DIR.mkdir(parents=True, exist_ok=True)
    PDFS_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)  # P2：chat 出图目录
    vision.start_background_worker()
    recovered = vision.recover_pending_jobs()
    print("[startup] db ready at", db.DB_PATH)
    if migration["status"] == "imported":
        print("[startup] legacy model config migrated capabilities=", migration["capabilities"])
    if recovered:
        print("[startup] recovered vision jobs", recovered)
    # 存量 has_image 补跑（后台一次性，不阻塞服务就绪；纯本地 PyMuPDF，百页级秒完）
    _spawn_has_image_backfill()


def _spawn_has_image_backfill() -> None:
    """启动时后台补跑 has_image IS NULL 的存量页；完成后扫描 fig_desc 预跑缺口。"""
    loop = asyncio.get_event_loop()

    def _scan_fig_prerun() -> None:
        try:
            recovered = vision.recover_pending_fig_preruns()
            if recovered:
                print(f"[startup] recovered fig_prerun jobs {recovered}")
        except Exception as e:  # noqa: BLE001 —— 预跑补漏失败不影响服务
            logging.warning("fig_prerun_startup_scan_failed: %s", e)

    def _job():
        try:
            done, total = backfill_has_image()
            print(f"[startup] has_image_backfill done={done}/{total}")
            loop.call_soon_threadsafe(_scan_fig_prerun)
        except Exception as e:  # noqa: BLE001 —— 补跑失败不影响服务
            logging.warning("has_image_backfill_failed: %s", e)

    loop.run_in_executor(None, _job)


@app.on_event("shutdown")
async def shutdown() -> None:
    # 退出时容忍中断；未完成的 pending/running 会在下次启动恢复。
    await vision.stop_background_worker()


# ---------- helpers ----------

def backfill_has_image() -> tuple[int, int]:
    """存量补跑：扫描 has_image IS NULL 的页，逐页物理检测回填。返回 (done, total)。

    - 按 doc 分组、PDF 只开一次（同 doc 各页共享文件句柄），纯本地 PyMuPDF，快
    - 单个 PDF 损坏/缺失：该 doc 剩余页保持 NULL（下次启动再试），不影响其他 doc
    - 幂等：只碰 NULL 行，已检测过的（上传时已写入）不重算
    """
    import pymupdf

    conn = db.get_conn()
    rows = conn.execute(
        """SELECT dp.doc_id, dp.page_no, d.pdf_path FROM document_pages dp
           JOIN documents d ON d.id = dp.doc_id
           WHERE dp.has_image IS NULL
           ORDER BY dp.doc_id, dp.page_no"""
    ).fetchall()
    total = len(rows)
    if not total:
        return 0, 0

    by_doc: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        by_doc.setdefault(r["doc_id"], []).append(r)

    done = 0
    for doc_id, pages in by_doc.items():
        pdf_path = pages[0]["pdf_path"]
        try:
            doc = pymupdf.open(pdf_path)
        except Exception as e:  # noqa: BLE001 —— 单文件损坏不阻塞整体补跑
            logging.warning("has_image_backfill open_failed doc=%s path=%s: %s", doc_id, pdf_path, e)
            continue
        try:
            for r in pages:
                page_no = r["page_no"]
                if 1 <= page_no <= doc.page_count:
                    flag = page_has_image(doc[page_no - 1])
                else:  # 页码越界（异常数据）：记 0，不再反复重试
                    flag = 0
                conn.execute(
                    "UPDATE document_pages SET has_image=? WHERE doc_id=? AND page_no=?",
                    (flag, doc_id, page_no),
                )
                done += 1
        finally:
            doc.close()
    conn.commit()
    return done, total


def _row_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def _get_subject(subject_id: int) -> dict:
    row = db.get_conn().execute(
        "SELECT * FROM subjects WHERE id=?", (subject_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, f"subject {subject_id} 不存在")
    return _row_dict(row)


def _get_folder(folder_id: int) -> dict:
    row = db.get_conn().execute(
        "SELECT * FROM folders WHERE id=?", (folder_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, f"folder {folder_id} 不存在")
    return _row_dict(row)


def _get_doc(doc_id: int) -> dict:
    row = db.get_conn().execute(
        "SELECT * FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, f"document {doc_id} 不存在")
    return _row_dict(row)


def folder_depth(folder_id: int) -> int:
    """文件夹深度：科目根级=1，嵌套一层=2。≤2 层校验用。"""
    depth = 1
    cur = _get_folder(folder_id)
    while cur["parent_id"] is not None:
        cur = _get_folder(cur["parent_id"])
        depth += 1
    return depth


# ---------- subjects ----------

class SubjectIn(BaseModel):
    name: str


class SubjectPatch(BaseModel):
    name: Optional[str] = None
    sort_order: Optional[int] = None


@app.post("/api/subjects")
def create_subject(body: SubjectIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "科目名不能为空")
    conn = db.get_conn()
    sort = db.next_sort_order("subjects")
    cur = conn.execute(
        "INSERT INTO subjects(name, sort_order) VALUES(?,?)", (name, sort)
    )
    conn.commit()
    return {"subject_id": cur.lastrowid}


@app.get("/api/subjects")
def list_subjects():
    conn = db.get_conn()
    rows = conn.execute(
        """SELECT s.*, (SELECT COUNT(*) FROM documents d WHERE d.subject_id = s.id) AS doc_count
           FROM subjects s ORDER BY s.sort_order, s.id"""
    ).fetchall()
    return [
        {"id": r["id"], "name": r["name"], "sort_order": r["sort_order"],
         "doc_count": r["doc_count"], "created_at": r["created_at"]}
        for r in rows
    ]


@app.patch("/api/subjects/{subject_id}")
def patch_subject(subject_id: int, body: SubjectPatch):
    _get_subject(subject_id)
    conn = db.get_conn()
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "科目名不能为空")
        conn.execute("UPDATE subjects SET name=? WHERE id=?", (name, subject_id))
    if body.sort_order is not None:
        conn.execute(
            "UPDATE subjects SET sort_order=? WHERE id=?", (body.sort_order, subject_id)
        )
    conn.commit()
    return {"ok": True}


@app.delete("/api/subjects/{subject_id}")
def delete_subject(subject_id: int):
    """只删空科目；非空返回 409 + 统计（防误删整科课件与对话）。"""
    _get_subject(subject_id)
    conn = db.get_conn()
    n_docs = conn.execute(
        "SELECT COUNT(*) AS n FROM documents WHERE subject_id=?", (subject_id,)
    ).fetchone()["n"]
    n_folders = conn.execute(
        "SELECT COUNT(*) AS n FROM folders WHERE subject_id=?", (subject_id,)
    ).fetchone()["n"]
    if n_docs or n_folders:
        raise HTTPException(
            409,
            detail={"msg": "科目非空，先移走内容", "doc_count": n_docs, "folder_count": n_folders},
        )
    conn.execute("DELETE FROM subjects WHERE id=?", (subject_id,))
    conn.commit()
    return {"ok": True}


# ---------- folders ----------

class FolderIn(BaseModel):
    subject_id: int
    parent_id: Optional[int] = None
    name: str


class FolderPatch(BaseModel):
    name: Optional[str] = None
    sort_order: Optional[int] = None
    parent_id: Optional[int] = None


@app.post("/api/folders")
def create_folder(body: FolderIn):
    _get_subject(body.subject_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "文件夹名不能为空")
    if body.parent_id is not None:
        parent = _get_folder(body.parent_id)
        if parent["subject_id"] != body.subject_id:
            raise HTTPException(422, "parent_id 必须属于同一科目")
        # ≤2 层校验：父文件夹深度已达 2 时，再嵌套即第 3 层 → 拒绝
        if folder_depth(body.parent_id) >= 2:
            raise HTTPException(400, "文件夹最多嵌套 2 层")
    conn = db.get_conn()
    sort = db.next_child_sort_order(body.subject_id, body.parent_id)
    cur = conn.execute(
        "INSERT INTO folders(subject_id, parent_id, name, sort_order) VALUES(?,?,?,?)",
        (body.subject_id, body.parent_id, name, sort),
    )
    conn.commit()
    return {"folder_id": cur.lastrowid}


@app.patch("/api/folders/{folder_id}")
def patch_folder(folder_id: int, body: FolderPatch):
    folder = _get_folder(folder_id)
    conn = db.get_conn()
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "文件夹名不能为空")
        conn.execute("UPDATE folders SET name=? WHERE id=?", (name, folder_id))
    if body.sort_order is not None:
        conn.execute(
            "UPDATE folders SET sort_order=? WHERE id=?", (body.sort_order, folder_id)
        )
    if "parent_id" in body.model_fields_set:
        new_parent = body.parent_id
        if new_parent is not None:
            parent = _get_folder(new_parent)
            if parent["subject_id"] != folder["subject_id"]:
                raise HTTPException(422, "parent_id 必须属于同一科目")
            if new_parent == folder_id:
                raise HTTPException(422, "parent_id 不能指向自身")
            if folder_depth(new_parent) >= 2:
                raise HTTPException(422, "文件夹最多嵌套 2 层")
            # 防环：沿 parent 链向上不允许经过自身
            cur = parent
            while cur is not None:
                if cur["id"] == folder_id:
                    raise HTTPException(422, "parent_id 形成循环")
                cur = _get_folder(cur["parent_id"]) if cur["parent_id"] is not None else None
        # 移动后自身深度（含子树）不能超过 2：new_parent 深度 + 自身子树高度 ≤ 2
        if new_parent is not None:
            subtree_h = 1
            frontier = [folder_id]
            while frontier:
                rows = conn.execute(
                    "SELECT id FROM folders WHERE parent_id IN (%s)" % ",".join("?" * len(frontier)),
                    frontier,
                ).fetchall()
                frontier = [r["id"] for r in rows]
                if frontier:
                    subtree_h += 1
            if folder_depth(new_parent) + subtree_h > 2:
                raise HTTPException(422, "文件夹最多嵌套 2 层")
        conn.execute(
            "UPDATE folders SET parent_id=? WHERE id=?", (new_parent, folder_id)
        )
    conn.commit()
    return {"ok": True}


@app.delete("/api/folders/{folder_id}")
def delete_folder(folder_id: int):
    """只删空文件夹；非空（有子文件夹或有课件）返回 409 + 子项数量。"""
    _get_folder(folder_id)
    conn = db.get_conn()
    n_sub = conn.execute(
        "SELECT COUNT(*) AS n FROM folders WHERE parent_id=?", (folder_id,)
    ).fetchone()["n"]
    n_docs = conn.execute(
        "SELECT COUNT(*) AS n FROM documents WHERE folder_id=?", (folder_id,)
    ).fetchone()["n"]
    if n_sub or n_docs:
        raise HTTPException(
            409,
            detail={"msg": "文件夹非空，先移走内容", "subfolder_count": n_sub, "doc_count": n_docs},
        )
    conn.execute("DELETE FROM folders WHERE id=?", (folder_id,))
    conn.commit()
    return {"ok": True}


# ---------- tree ----------

@app.get("/api/tree")
def get_tree(subject_id: int):
    """科目下文件夹树 + 课件列表（侧栏整棵树一次拉全）。"""
    _get_subject(subject_id)
    conn = db.get_conn()
    folders = [
        _row_dict(r)
        for r in conn.execute(
            "SELECT * FROM folders WHERE subject_id=? ORDER BY sort_order, id", (subject_id,)
        )
    ]
    docs = []
    for r in conn.execute(
        "SELECT * FROM documents WHERE subject_id=? ORDER BY sort_order, id", (subject_id,)
    ):
        d = _row_dict(r)
        d["failed_pages"] = json.loads(d.get("failed_pages") or "[]")
        d["vision"] = {
            "status": d.get("vision_status", "done"),
            "done": d.get("vision_done", 0),
            "total": d.get("vision_total", 0),
            "failed_pages": d["failed_pages"],
        }
        docs.append(d)
    # 扁平返回（前端组装树）：folders 全量含 parent_id，documents 全量含 folder_id
    return {
        "subject_id": subject_id,
        "folders": folders,
        "documents": docs,
    }


class TreeOrderItem(BaseModel):
    type: Literal["folder", "document"]
    id: int


class TreeOrderPatch(BaseModel):
    subject_id: int
    parent_id: Optional[int] = None
    items: list[TreeOrderItem]


@app.patch("/api/tree/order")
def patch_tree_order(body: TreeOrderPatch):
    """原子重写同一容器内文件夹/课件共享的 sort_order。"""
    _get_subject(body.subject_id)
    if body.parent_id is not None:
        parent = _get_folder(body.parent_id)
        if parent["subject_id"] != body.subject_id:
            raise HTTPException(422, "parent_id 必须属于同一科目")

    conn = db.get_conn()
    expected_folders = {
        r["id"] for r in conn.execute(
            "SELECT id FROM folders WHERE subject_id=? AND parent_id IS ?",
            (body.subject_id, body.parent_id),
        )
    }
    expected_docs = {
        r["id"] for r in conn.execute(
            "SELECT id FROM documents WHERE subject_id=? AND folder_id IS ?",
            (body.subject_id, body.parent_id),
        )
    }
    got_folders = {item.id for item in body.items if item.type == "folder"}
    got_docs = {item.id for item in body.items if item.type == "document"}
    def order_error(msg: str):
        return {
            "msg": msg,
            "expected_folders": sorted(expected_folders),
            "expected_docs": sorted(expected_docs),
            "got_folders": sorted(got_folders),
            "got_docs": sorted(got_docs),
            "subject_id": body.subject_id,
            "parent_id": body.parent_id,
            "items": [item.model_dump() for item in body.items],
        }

    if len(body.items) != len(got_folders) + len(got_docs):
        detail = order_error("items 不允许重复")
        logging.warning("tree_order_validation_failed %s", json.dumps(detail, ensure_ascii=False))
        raise HTTPException(422, detail)
    if got_folders != expected_folders or got_docs != expected_docs:
        detail = order_error("items 必须完整且全部属于指定容器")
        logging.warning("tree_order_validation_failed %s", json.dumps(detail, ensure_ascii=False))
        raise HTTPException(422, detail)

    try:
        for index, item in enumerate(body.items):
            table = "folders" if item.type == "folder" else "documents"
            conn.execute(f"UPDATE {table} SET sort_order=? WHERE id=?", (index, item.id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"ok": True}


class TreeMoveContainer(BaseModel):
    subject_id: int
    parent_id: Optional[int] = None
    items: list[TreeOrderItem]


class TreeMovePatch(BaseModel):
    active: TreeOrderItem
    to_subject_id: int
    to_parent_id: Optional[int] = None
    containers: list[TreeMoveContainer]


@app.patch("/api/tree/move")
def patch_tree_move(body: TreeMovePatch):
    """在一个事务中完成课件归属移动与所有受影响容器的混排。"""
    conn = db.get_conn()
    active_table = "folders" if body.active.type == "folder" else "documents"
    parent_column = "parent_id" if body.active.type == "folder" else "folder_id"
    active_row = conn.execute(
        f"SELECT id,subject_id,{parent_column} AS parent_id FROM {active_table} WHERE id=?",
        (body.active.id,),
    ).fetchone()
    if not active_row:
        raise HTTPException(404, f"{body.active.type} {body.active.id} 不存在")

    _get_subject(body.to_subject_id)
    if body.to_parent_id is not None:
        target_parent = _get_folder(body.to_parent_id)
        if target_parent["subject_id"] != body.to_subject_id:
            raise HTTPException(422, "to_parent_id 必须属于目标科目")

    old_key = (active_row["subject_id"], active_row["parent_id"])
    new_key = (body.to_subject_id, body.to_parent_id)
    if body.active.type == "folder" and new_key != old_key:
        # 文件夹跨容器移动：允许同科目重排与跨科目迁移（跨科目仅允许落到目标科目根级），
        # 保持 ≤2 层嵌套；子树（子文件夹/课件）随 folder 的 subject_id 联动更新。
        if body.to_subject_id != active_row["subject_id"] and body.to_parent_id is not None:
            raise HTTPException(422, "文件夹跨科目只能移到目标科目根级")
        if body.to_parent_id is not None:
            target_parent = _get_folder(body.to_parent_id)
            if target_parent["subject_id"] != body.to_subject_id:
                raise HTTPException(422, "to_parent_id 必须属于目标科目")
            if folder_depth(body.to_parent_id) >= 2:
                raise HTTPException(422, "文件夹最多嵌套 2 层")
            if body.to_parent_id == body.active.id:
                raise HTTPException(422, "不能把文件夹移入自身")
        # active 子树高度（自身=1，每层嵌套 +1）
        subtree_h = 1
        frontier = [body.active.id]
        while frontier:
            rows = conn.execute(
                "SELECT id FROM folders WHERE parent_id IN (%s)" % ",".join("?" * len(frontier)),
                frontier,
            ).fetchall()
            frontier = [r["id"] for r in rows]
            if frontier:
                subtree_h += 1
        target_depth = 1 if body.to_parent_id is None else folder_depth(body.to_parent_id) + 1
        if target_depth + subtree_h - 1 > 2:
            raise HTTPException(422, "文件夹最多嵌套 2 层")

    submitted: dict[tuple[int, Optional[int]], list[TreeOrderItem]] = {}
    for container in body.containers:
        key = (container.subject_id, container.parent_id)
        if key in submitted:
            raise HTTPException(422, "containers 不允许重复")
        _get_subject(container.subject_id)
        if container.parent_id is not None:
            parent = _get_folder(container.parent_id)
            if parent["subject_id"] != container.subject_id:
                raise HTTPException(422, "container parent_id 必须属于同一科目")
        submitted[key] = container.items

    required_keys = {old_key, new_key}
    if not required_keys.issubset(submitted):
        raise HTTPException(422, {
            "msg": "containers 必须包含源和目标容器",
            "expected": sorted([list(key) for key in required_keys], key=str),
            "got": sorted([list(key) for key in submitted], key=str),
        })

    validation_errors = []
    for (subject_id, parent_id), items in submitted.items():
        expected_folders = {
            r["id"] for r in conn.execute(
                "SELECT id FROM folders WHERE subject_id=? AND parent_id IS ?",
                (subject_id, parent_id),
            )
        }
        expected_docs = {
            r["id"] for r in conn.execute(
                "SELECT id FROM documents WHERE subject_id=? AND folder_id IS ?",
                (subject_id, parent_id),
            )
        }
        if body.active.type == "document" and old_key != new_key:
            if (subject_id, parent_id) == old_key:
                expected_docs.discard(body.active.id)
            if (subject_id, parent_id) == new_key:
                expected_docs.add(body.active.id)
        if body.active.type == "folder" and old_key != new_key:
            if (subject_id, parent_id) == old_key:
                expected_folders.discard(body.active.id)
            if (subject_id, parent_id) == new_key:
                expected_folders.add(body.active.id)
        got_folders = {item.id for item in items if item.type == "folder"}
        got_docs = {item.id for item in items if item.type == "document"}
        duplicate = len(items) != len(got_folders) + len(got_docs)
        if duplicate or got_folders != expected_folders or got_docs != expected_docs:
            validation_errors.append({
                "subject_id": subject_id,
                "parent_id": parent_id,
                "expected_folders": sorted(expected_folders),
                "expected_docs": sorted(expected_docs),
                "got_folders": sorted(got_folders),
                "got_docs": sorted(got_docs),
                "items": [item.model_dump() for item in items],
                "duplicate": duplicate,
            })

    if validation_errors:
        detail = {"msg": "受影响容器的 items 必须完整且唯一", "containers": validation_errors}
        logging.warning("tree_move_validation_failed %s", json.dumps(detail, ensure_ascii=False))
        raise HTTPException(422, detail)

    try:
        if body.active.type == "document" and old_key != new_key:
            conn.execute(
                "UPDATE documents SET subject_id=?, folder_id=? WHERE id=?",
                (body.to_subject_id, body.to_parent_id, body.active.id),
            )
        if body.active.type == "folder" and old_key != new_key:
            conn.execute(
                "UPDATE folders SET subject_id=?, parent_id=? WHERE id=?",
                (body.to_subject_id, body.to_parent_id, body.active.id),
            )
            if body.to_subject_id != active_row["subject_id"]:
                # 跨科目：子树整体跟随 —— 后代文件夹与所有课件都改挂到目标科目
                subtree_ids = [body.active.id]
                i = 0
                while i < len(subtree_ids):
                    fid = subtree_ids[i]
                    i += 1
                    children = conn.execute(
                        "SELECT id FROM folders WHERE parent_id=?", (fid,)
                    ).fetchall()
                    subtree_ids.extend(r["id"] for r in children)
                ph = ",".join("?" * len(subtree_ids))
                conn.execute(
                    f"UPDATE folders SET subject_id=? WHERE id IN ({ph})",
                    [body.to_subject_id, *subtree_ids],
                )
                conn.execute(
                    f"UPDATE documents SET subject_id=? WHERE folder_id IN ({ph})",
                    [body.to_subject_id, *subtree_ids],
                )
        for items in submitted.values():
            for index, item in enumerate(items):
                table = "folders" if item.type == "folder" else "documents"
                conn.execute(f"UPDATE {table} SET sort_order=? WHERE id=?", (index, item.id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"ok": True}


@app.get("/api/docs/{doc_id}/pages")
def get_doc_pages(doc_id: int):
    """每页实际宽高（pt）：前端按页真实宽高比渲染（A4 竖版 / PPT 横版混排自适应）。

    数据来自上传时 PyMuPDF 抽取的 document_pages.width/height，
    按 page_no 排序返回；前端兜底 16:9 只在拿不到本接口时生效。
    """
    conn = db.get_conn()
    row = conn.execute("SELECT id FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        raise HTTPException(400, f"document {doc_id} 不存在")
    doc = conn.execute(
        "SELECT vision_status, vision_done, vision_total, failed_pages FROM documents WHERE id=?",
        (doc_id,),
    ).fetchone()
    rows = conn.execute(
        "SELECT page_no, width, height, source, text, fig_desc FROM document_pages"
        " WHERE doc_id=? ORDER BY page_no",
        (doc_id,),
    ).fetchall()
    failed_pages = json.loads(doc["failed_pages"] or "[]")
    return {
        "doc_id": doc_id,
        "vision": {
            "status": doc["vision_status"],
            "done": doc["vision_done"],
            "total": doc["vision_total"],
            "failed_pages": failed_pages,
        },
        "pages": [
            {
                "page_no": r["page_no"],
                "width": r["width"],
                "height": r["height"],
                "source": r["source"],
                "has_text": bool(r["text"]),
                # 图形描述缓存状态（chat 按需识读写入）；前端暂不展示，字段备着
                "has_fig": bool(r["fig_desc"]),
            }
            for r in rows
        ],
    }


@app.get("/api/docs/{doc_id}/vision/status")
def get_vision_status(doc_id: int):
    _get_doc(doc_id)
    return {"doc_id": doc_id, "vision": vision.get_doc_vision_status(doc_id)}


# ---------- documents（PATCH 移动/排序） ----------

class DocPatch(BaseModel):
    subject_id: Optional[int] = None
    folder_id: Optional[int] = None
    sort_order: Optional[int] = None
    name: Optional[str] = None


@app.patch("/api/documents/{doc_id}")
def patch_document(doc_id: int, body: DocPatch):
    """移动课件/排序/重命名。跨科目移动时清空 folder_id 并校验归属一致性。"""
    doc = _get_doc(doc_id)
    conn = db.get_conn()

    new_subject = body.subject_id if body.subject_id is not None else doc["subject_id"]
    # 显式 folder_id:null → 移到科目根级；未传 → 保持原文件夹
    new_folder = body.folder_id if "folder_id" in body.model_fields_set else doc["folder_id"]

    # 跨科目移动：后端清空 folder_id（原文件夹不属于新科目）
    if body.subject_id is not None and body.subject_id != doc["subject_id"]:
        _get_subject(body.subject_id)
        new_folder = None

    if new_folder is not None:
        folder = _get_folder(new_folder)
        if folder["subject_id"] != new_subject:
            raise HTTPException(422, "folder_id 与 subject_id 归属不一致")

    conn.execute(
        """UPDATE documents SET subject_id=?, folder_id=?,
           sort_order=COALESCE(?, sort_order),
           filename=COALESCE(?, filename)
           WHERE id=?""",
        (
            new_subject,
            new_folder,
            body.sort_order,
            body.name.strip() if body.name else None,
            doc_id,
        ),
    )
    conn.commit()
    return {"ok": True}


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: int):
    """删课件：连带删页面索引、对话与本地文件。"""
    doc = _get_doc(doc_id)
    conn = db.get_conn()
    conn.execute("DELETE FROM document_pages WHERE doc_id=?", (doc_id,))
    conn.execute("DELETE FROM conversations WHERE doc_id=?", (doc_id,))
    conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    conn.commit()
    for p in (doc["orig_path"], doc["pdf_path"]):
        try:
            os.remove(p)
        except OSError as e:
            # 文件删不掉不阻塞接口返回；但必须留痕，否则盘上孤儿无从排查（曾静默吞掉导致残留）
            logging.warning("delete_document: 清理本地文件失败 doc_id=%s path=%s: %s", doc_id, p, e)
    return {"ok": True}


# ---------- upload ----------

@app.post("/api/upload")
async def upload(
    subject_id: int = Form(...),
    file: UploadFile = File(...),
    folder_id: Optional[int] = Form(None),
):
    """上传 PDF/PPT/Word：originals/ →（非 PDF）convert 转 pdfs/ → 逐页抽取入库。

    空文本页不再同步读图；只初始化 vision 进度并入后台队列，立即返回。
    出参：{doc_id, page_count, en_pages, empty_pages, vision_pages, vision}（技术文档 §5）
    - vision：pending/running/done/partial 进度；前端轮询 /api/docs/{id}/pages 或 /vision/status
    - empty_pages：上传当下仍无文本的页（读图后台回填后消失）
    """
    _get_subject(subject_id)
    if folder_id is not None:
        folder = _get_folder(folder_id)
        if folder["subject_id"] != subject_id:
            raise HTTPException(422, "folder_id 与 subject_id 归属不一致")

    filename = file.filename or "untitled.pdf"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(415, f"仅支持 PDF/PPTX/DOCX/PPT/DOC 上传，收到 {ext or '无后缀'}")

    # 读文件（≤100MB 校验）
    payload = await file.read()
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "文件超过 100MB 上限")
    if len(payload) == 0:
        raise HTTPException(422, "空文件")

    # originals/ 存原文件（uuid 前缀防重名）
    stem = re.sub(r"[^\w\-.]+", "_", Path(filename).stem)[:64] or "doc"
    token = uuid.uuid4().hex[:8]
    orig_path = ORIGINALS_DIR / f"{token}_{stem}{ext}"
    pdf_path = PDFS_DIR / f"{token}_{stem}.pdf"
    with open(orig_path, "wb") as f:
        f.write(payload)

    if ext == ".pdf":
        shutil.copyfile(orig_path, pdf_path)
    else:
        # M2：LibreOffice headless 转换。子进程调用放线程池，不阻塞事件循环。
        try:
            lo_out = await asyncio.to_thread(
                convert.convert_to_pdf, orig_path, PDFS_DIR
            )
            os.replace(lo_out, pdf_path)  # LibreOffice 输出名 → token 规范名
        except Exception as e:
            os.remove(orig_path)
            raise HTTPException(500, f"转换失败：{e}")

    # 逐页抽取 + 尺寸 + 语言检测 → 入库
    try:
        pages = extract_pdf(str(pdf_path))
    except Exception as e:
        os.remove(orig_path)
        os.remove(pdf_path)
        raise HTTPException(500, f"PDF 解析失败：{e}")

    conn = db.get_conn()
    sort = db.next_child_sort_order(subject_id, folder_id)
    cur = conn.execute(
        """INSERT INTO documents(subject_id, folder_id, filename, orig_path, pdf_path,
                                 page_count, sort_order)
           VALUES(?,?,?,?,?,?,?)""",
        (subject_id, folder_id, filename, str(orig_path), str(pdf_path), len(pages), sort),
    )
    doc_id = cur.lastrowid
    en_pages = 0
    empty_pages: list[int] = []
    for p in pages:
        if p.lang == "en":
            en_pages += 1
        if not p.text:
            empty_pages.append(p.page_no)
        conn.execute(
            "INSERT INTO document_pages(doc_id, page_no, text, lang, source, width, height, has_image)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (doc_id, p.page_no, p.text, p.lang, p.source, p.width, p.height, p.has_image),
        )
    conn.commit()

    # M2+: 空文本页进入异步 vision 管线；上传立即返回，不阻塞数分钟读图。
    vision.init_doc_vision_job(doc_id, len(empty_pages))
    if empty_pages:
        vision.enqueue_vision_doc(doc_id)
    # 上传完成后后台预跑图形描述，消除首次提问现场识图延迟。
    # 用 create_task 进入同一个全局串行队列；实际页面筛选/40 页上限在 vision worker 内完成。
    if any(int(p.has_image or 0) == 1 for p in pages):
        asyncio.create_task(vision.enqueue_fig_prerun_async(doc_id), name=f"fig-prerun-enqueue-{doc_id}")

    return {
        "doc_id": doc_id,
        "page_count": len(pages),
        "en_pages": en_pages,
        "empty_pages": empty_pages,
        "vision_pages": [],
        "vision": vision.get_doc_vision_status(doc_id),
    }


# ---------- vision 手动重读（上传时读图失败的页可重试） ----------

class VisionRereadIn(BaseModel):
    pages: Optional[list[int]] = None  # 不传 = 全部空文本页


@app.post("/api/docs/{doc_id}/vision/reread")
async def vision_reread(doc_id: int, body: VisionRereadIn):
    """对空文本页重跑 vision 读图（body 可选 pages，默认全部空文本页）。

    返回 {vision_pages, empty_pages}：本次成功回填页 / 仍为空的页。
    """
    doc = _get_doc(doc_id)
    if not os.path.exists(doc["pdf_path"]):
        raise HTTPException(410, "PDF 文件缺失（本地 data/pdfs 被清理？）")

    conn = db.get_conn()
    if body.pages:
        # 指定页码须存在于该课件
        placeholders = ",".join("?" for _ in body.pages)
        rows = conn.execute(
            f"SELECT page_no FROM document_pages WHERE doc_id=? AND page_no IN ({placeholders})",
            (doc_id, *body.pages),
        ).fetchall()
        found = {r["page_no"] for r in rows}
        missing = sorted(set(body.pages) - found)
        if missing:
            raise HTTPException(404, f"页码不存在：{missing}")
        targets = sorted(found)
    else:
        targets = [
            r["page_no"]
            for r in conn.execute(
                "SELECT page_no FROM document_pages WHERE doc_id=? AND text='' ORDER BY page_no",
                (doc_id,),
            ).fetchall()
        ]

    vision_pages: list[int] = []
    failed_pages: list[int] = []
    if targets:
        try:
            vision._save_progress(doc_id, status="running", done=0, total=len(targets), failed_pages=[])
            vision_pages, failed_pages = await vision.transcribe_pages(
                doc_id, doc["pdf_path"], targets, update_progress=True
            )
            vision._save_progress(
                doc_id,
                status="partial" if failed_pages else "done",
                done=len(targets),
                total=len(targets),
                failed_pages=failed_pages,
            )
        except Exception as e:  # noqa: BLE001
            logging.warning("vision_reread_failed doc=%s: %s", doc_id, e)
            failed_pages = list(targets)

    empty_pages = [
        r["page_no"]
        for r in conn.execute(
            "SELECT page_no FROM document_pages WHERE doc_id=? AND text='' ORDER BY page_no",
            (doc_id,),
        ).fetchall()
    ]
    return {"vision_pages": vision_pages, "empty_pages": empty_pages}


# ---------- fig_desc 手动预跑/重跑 ----------

class FigPrerunIn(BaseModel):
    force: bool = False  # true：清空该 doc 全部 fig_desc 后重识 has_image=1 页


@app.post("/api/docs/{doc_id}/fig/prerun")
async def fig_prerun(doc_id: int, body: FigPrerunIn | None = None):
    """手动触发该课件 fig_desc 后台预跑。

    默认只跑 `has_image=1 AND fig_desc IS NULL` 的页；`force:true` 先清空该 doc 全部
    fig_desc 后重识 has_image=1 页。实际执行复用 vision 全局串行队列，单课件最多预跑 40 页。
    """
    _get_doc(doc_id)
    force = bool(body.force) if body else False
    conn = db.get_conn()
    if force:
        conn.execute("UPDATE document_pages SET fig_desc=NULL WHERE doc_id=?", (doc_id,))
        conn.commit()
    _, targets, total_pending = vision._fig_prerun_targets(doc_id)
    queued = False
    if total_pending:
        queued = vision.enqueue_fig_prerun(doc_id)
    return {
        "doc_id": doc_id,
        "queued": queued,
        "force": force,
        "pending_pages": total_pending,
        "will_run_pages": targets,
        "limit": vision.FIG_PRERUN_PAGE_LIMIT,
    }


# ---------- docs（PDF 流） ----------

@app.get("/api/docs/{doc_id}")
def get_doc_pdf(doc_id: int):
    doc = _get_doc(doc_id)
    path = doc["pdf_path"]
    if not os.path.exists(path):
        raise HTTPException(410, "PDF 文件缺失（本地 data/pdfs 被清理？）")
    # RFC 5987：ASCII 兜底 filename + UTF-8 编码 filename*（中文文件名直接塞 header 会 latin-1 炸）
    ascii_name = re.sub(r"[^\w\-.]+", "_", doc["filename"], flags=re.ASCII) or "document.pdf"
    utf8_name = quote(doc["filename"])
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"},
    )


# ---------- chat（SSE） ----------

class ChatIn(BaseModel):
    doc_id: int
    pages: list[int]
    question: str
    history: Optional[list[dict]] = None
    model_config_id: Optional[int] = None


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# 图形按需识读触发词：问题命中且勾选页 fig_desc 未缓存时，chat 前先跑 vision 图形描述
FIGURE_KEYWORD_RE = re.compile(
    r"图|原理图|电路|波形|曲线|示意|figure|diagram|circuit|waveform|graph|plot",
    re.IGNORECASE,
)

# 图形按需识读触发（2026-08-19 重构：物理检测替代 LLM 预判）：
# 「页面是否有图」是 PDF 结构层面的事实，上传时 PyMuPDF 物理检测写入 has_image
# （≥100px 嵌入位图=1），不再让 LLM 看着文字层猜——文字层天然不含图内信息，
# 猜错 = 模型看不到电路图里的参数，瞎猜答案。两级触发：
# 1) 快速通道：问题命中图形关键词 → 勾选页 fig_desc 未缓存的都识（有图没图让 vision 说）；
# 2) 物理检测通道：关键词未命中，但勾选页 has_image=1 且 fig_desc IS NULL → 直接识
#    （学生在解题、数据可能就在图里，漏的代价远大于一次 vision 调用）；
#    has_image=0 或 fig_desc 已缓存（含空串「无图形」）→ 跳过。
# 描述写入 fig_desc 永久缓存；失败静默跳过（保持 NULL 下次再试）。

# P2：出图标记 —— 独立一行，允许中英文冒号/方括号两侧空白
# [DRAW: 英文图片描述] —— 几何/立体/示意（image-model 生图）
DRAW_MARKER_RE = re.compile(
    r"[ \t]*\[DRAW[:：]\s*(?P<desc>[^\]\r\n]{1,500})\][ \t]*(?:\r?\n)?",
    re.IGNORECASE,
)
# [PLOT: {"kind": ..., ...}] —— 函数/波形/布尔/电路（matplotlib/schemdraw 精确绘制）；
# 兼容纯文本指令 [PLOT: plot y=x^2 ...]
PLOT_MARKER_RE = re.compile(
    r"[ \t]*\[PLOT[:：]\s*(?P<spec>(?:\{.*?\}|[^\]]{1,600}))\][ \t]*(?:\r?\n)?",
    re.IGNORECASE | re.DOTALL,
)
# 两个标记合并扫描（缓冲剥离用）：取先出现者
_MARKER_RE = re.compile(
    r"[ \t]*\[(?:DRAW[:：]\s*(?P<desc>[^\]\r\n]{1,500})|"
    r"PLOT[:：]\s*(?P<spec>(?:\{.*?\}|[^\]]{1,600})))\][ \t]*(?:\r?\n)?",
    re.IGNORECASE | re.DOTALL,
)
# 尚未闭合的标记前缀（流式缓冲用）：检测到后扣住后续 delta 直到 ']' 出现或确认不是标记
_MARKER_PREFIX_RE = re.compile(r"[ \t]*\[(?:DRAW|PLOT)[:：]\s*[^\]]*$", re.IGNORECASE)
_MARKER_HEAD_RE = re.compile(r"[ \t]*\[(?:D?R?A?W?|P?L?O?T?)[:：]?\s*$", re.IGNORECASE)


def extract_draw_prompts(text: str) -> tuple[str, list[str]]:
    """从完整回答里剥离 [DRAW: ...] 标记，返回 (干净正文, 图片描述列表)。

    标记行整体移除（含其后换行），前端不显示原文；描述按出现顺序保留。
    """
    prompts = [m.group("desc").strip() for m in DRAW_MARKER_RE.finditer(text)]
    cleaned = DRAW_MARKER_RE.sub("", text)
    # 剥离后可能留下连续空行（标记独占一行时），压成最多两个换行
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip(), prompts


def extract_plot_specs(text: str) -> tuple[str, list[str]]:
    """从完整回答里剥离 [PLOT: ...] 标记，返回 (干净正文, 绘图指令列表)。"""
    specs = [m.group("spec").strip() for m in PLOT_MARKER_RE.finditer(text)]
    cleaned = PLOT_MARKER_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip(), specs


class DrawFilterBuffer:
    """流式 [DRAW]/[PLOT] 标记过滤器：保持打字机增量下发，标记片段永不外漏。

    原理：正常文本直接放行；遇到可能是标记前缀的尾部（如 `[DRAW: de`、
    `[PLOT: {"kind`，未闭合）先扣住，直到能判定——闭合即收集为完整标记
    （丢弃，不入正文流），或流结束/明显不是标记时原样放行。
    """

    def __init__(self) -> None:
        self.buf = ""        # 扣住的待判定文本
        self.prompts: list[str] = []  # [DRAW] 描述（保持原语义）
        self.plots: list[str] = []    # [PLOT] 内容（JSON 或纯文本指令）
        # 按出现顺序的完整标记序列：[("draw", desc) | ("plot", spec)]
        self.markers: list[tuple[str, str]] = []

    def feed(self, delta: str) -> str:
        self.buf += delta
        while True:
            # 1) 完整标记：剥掉（含尾随换行），内容进列表，继续扫剩余部分
            m = _MARKER_RE.search(self.buf)
            if m:
                if m.group("desc") is not None:
                    content = m.group("desc").strip()
                    self.prompts.append(content)
                    self.markers.append(("draw", content))
                else:
                    content = m.group("spec").strip()
                    self.plots.append(content)
                    self.markers.append(("plot", content))
                self.buf = self.buf[: m.start()] + self.buf[m.end():]
                continue
            # 2) 已出现 `[DRAW:` / `[PLOT:` 但未闭合：从标记起点起全部扣住
            p = _MARKER_PREFIX_RE.search(self.buf)
            if p:
                head, self.buf = self.buf[: p.start()], self.buf[p.start():]
                return head
            # 3) 尾部是标记的开头片段（`[` / `[D` / `[PL` / `[PLOT` …）：扣住尾部等下一个 delta
            h = _MARKER_HEAD_RE.search(self.buf)
            if h:
                head, self.buf = self.buf[: h.start()], self.buf[h.start():]
                return head
            # 4) 无标记痕迹：全部放行
            out, self.buf = self.buf, ""
            return out

    def flush(self) -> str:
        """流结束：剩余 buf 原样放行；但 `[DRAW:`/`[PLOT:` 起的未闭合片段（流被截断）丢弃。"""
        rest, self.buf = self.buf, ""
        p = _MARKER_PREFIX_RE.search(rest)
        if p:
            rest = rest[: p.start()]
        return rest


async def _handle_plot_marker(content: str) -> tuple[bytes, str, bool]:
    """[PLOT] 处理：plotgen 精确绘制；失败/geometry 降级 image-model（degraded=true）。"""
    try:
        img = await generate_plot(content)
        kind, _ = resolve_plot_instruction(content)
        source = "plot" if kind == "function" else kind  # 事件 source：plot/circuit/boolean/waveform
        return img, source, False
    except PlotError as e:
        logging.warning("plot_fallback_to_draw: %s", e)
        prompt = fallback_prompt(content)
        img = await generate_image(prompt)
        return img, "draw", True


async def _emit_media_events(markers: list[tuple[str, str]]):
    """逐个处理 [DRAW]/[PLOT] 标记并产出 SSE image 事件（正文流结束后调用）。

    成功：{type:"image", url:"/api/images/{file}", prompt, source, degraded?}
    失败：{type:"image", error:"...", source}—— 只降级图片，不影响正文。
    source：draw（image-model）| plot/circuit/boolean/waveform（精确绘制）。
    """
    counts = {"draw": 0, "plot": 0}
    for marker_type, content in markers:
        counts[marker_type] += 1
        if counts[marker_type] > 2:
            continue  # 一道题每种标记最多 2 个（与系统提示词约定一致）
        try:
            if marker_type == "draw":
                img = await generate_image(content)
                source, degraded, prompt = "draw", False, content
            else:
                img, source, degraded = await _handle_plot_marker(content)
                _, text = resolve_plot_instruction(content)
                # caption 优先（marker JSON 自带的中英双语图注），无则 instruction
                prompt = resolve_plot_caption(content) or text or content
            name = f"{uuid.uuid4().hex}.png"
            (IMAGES_DIR / name).write_bytes(img)
            logging.info("chat_image_generated source=%s file=%s bytes=%d", source, name, len(img))
            evt = {
                "type": "image",
                "url": f"/api/images/{name}",
                "prompt": prompt,
                "source": source,
            }
            if degraded:
                evt["degraded"] = True
            yield _sse("image", evt)
        except Exception as e:  # noqa: BLE001 —— 出图失败降级为 error 字段
            logging.warning("chat_image_failed kind=%s: %s", marker_type, e)
            yield _sse("image", {
                "type": "image", "error": str(e)[:300], "source": marker_type, "prompt": content,
            })


@app.post("/api/chat")
async def chat(body: ChatIn):
    """查 document_pages → 拼 prompt → 流式调 chat model → SSE（delta/image/done/error）。

    P2：回答中的 [DRAW: ...] / [PLOT: ...] 标记在正文流结束后触发出图
    （DRAW → image-model 生图；PLOT → matplotlib/schemdraw 精确绘制，
    失败降级生图并标注 degraded），以独立 image 事件推送；标记本身从
    delta 正文中剥离。
    """
    _get_doc(body.doc_id)
    if not body.pages:
        raise HTTPException(422, "pages 不能为空：至少勾选一页")
    if not body.question.strip():
        raise HTTPException(422, "question 不能为空")

    chat_provider = None
    # 老测试会用不接收 provider_config 的 fake stream；真实 stream_chat 必须先解析并固定配置。
    if "provider_config" in inspect.signature(stream_chat).parameters:
        import providers
        if body.model_config_id is None:
            try:
                chat_provider = providers.get_provider("chat")
            except providers.ModelConfigurationError as exc:
                raise HTTPException(400, str(exc)) from exc
        else:
            try:
                chat_provider = providers.get_provider_by_model_config_id(body.model_config_id, "chat")
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

    conn = db.get_conn()
    placeholders = ",".join("?" for _ in body.pages)
    rows = conn.execute(
        f"SELECT page_no, text, lang, fig_desc, has_image FROM document_pages"
        f" WHERE doc_id=? AND page_no IN ({placeholders}) ORDER BY page_no",
        (body.doc_id, *body.pages),
    ).fetchall()
    if not rows:
        raise HTTPException(404, "勾选的页码在该课件中不存在")
    found = {r["page_no"] for r in rows}
    missing = sorted(set(body.pages) - found)
    if missing:
        raise HTTPException(404, f"页码不存在：{missing}")

    page_dicts = [_row_dict(r) for r in rows]

    # 图形按需识读触发（物理检测版，废 LLM 预判）：
    # 1) 快速通道：问题命中图形关键词 → 未缓存页全识（vision 自会回「无图形」）；
    # 2) 物理检测通道：关键词未命中，但勾选页 has_image=1 且未识读 → 直接识
    #    （页面有图 + 学生在解题，数据可能就在图里；has_image=0/已缓存 → 不识）。
    # 等待期不改 SSE 协议，只是首 token 前多几秒；失败静默跳过（NULL 下次再试）。
    need_fig = [p["page_no"] for p in page_dicts if p.get("fig_desc") is None]
    if need_fig:
        keyword_hit = bool(FIGURE_KEYWORD_RE.search(body.question))
        if keyword_hit:
            targets = need_fig
            source = "keyword"
        else:
            targets = [
                no for no in need_fig
                if int(
                    next(p for p in page_dicts if p["page_no"] == no).get("has_image") or 0
                ) == 1
            ]
            source = "has_image"
        if targets:
            doc = db.get_conn().execute(
                "SELECT pdf_path FROM documents WHERE id=?", (body.doc_id,)
            ).fetchone()
            if doc and os.path.exists(doc["pdf_path"]):
                t0 = time.monotonic()
                saved = await vision.describe_doc_figures(
                    body.doc_id, doc["pdf_path"], targets
                )
                logging.info(
                    "chat_fig_desc doc=%s pages=%s saved=%s elapsed=%.1fs source=%s",
                    body.doc_id, targets, saved, time.monotonic() - t0, source,
                )
                if saved:
                    # 重查勾选页，把刚缓存的 fig_desc 带进 prompt
                    rows = conn.execute(
                        f"SELECT page_no, text, lang, fig_desc, has_image FROM document_pages"
                        f" WHERE doc_id=? AND page_no IN ({placeholders}) ORDER BY page_no",
                        (body.doc_id, *body.pages),
                    ).fetchall()
                    page_dicts = [_row_dict(r) for r in rows]

    messages = build_messages(page_dicts, body.question.strip(), body.history)

    async def gen():
        usage: dict = {}
        filt = DrawFilterBuffer()  # P2：流式剥离 [DRAW] 标记，保持打字机增量
        try:
            stream_kwargs: dict[str, object] = {"usage_out": usage}
            if "provider_config" in inspect.signature(stream_chat).parameters:
                stream_kwargs["provider_config"] = chat_provider
            async for delta in stream_chat(messages, **stream_kwargs):  # type: ignore[arg-type]
                visible = filt.feed(delta)
                if visible:
                    yield _sse("delta", {"type": "delta", "content": visible})
            tail = filt.flush()
            if tail:
                yield _sse("delta", {"type": "delta", "content": tail})
            # 正文全部发完后再出图（生图慢 15-40s、绘图秒出，都不阻塞正文流；
            # 前端先渲染正文，图片后到）
            if filt.markers:
                async for evt in _emit_media_events(filt.markers):
                    yield evt
            yield _sse("done", {"type": "done", "usage": usage})
        except Exception as e:  # noqa: BLE001 —— SSE 约定的 error 事件
            yield _sse("error", {"type": "error", "message": str(e)})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 反代不缓冲，保证打字机效果
        },
    )


# ---------- images 静态服务（P2：chat 出图） ----------

@app.get("/api/images/{filename}")
def get_image(filename: str):
    """chat 生成的图片文件流。文件名只允许 uuid hex + .png，防路径穿越。"""
    if not re.fullmatch(r"[0-9a-f]{32}\.png", filename):
        raise HTTPException(404, "图片不存在")
    path = IMAGES_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "图片不存在")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


# ---------- conversations（M1 落库，前端 M3 接） ----------

class ConvIn(BaseModel):
    doc_id: int
    pages: list[int]
    messages: list[dict]


@app.post("/api/conversations")
def save_conversation(body: ConvIn):
    _get_doc(body.doc_id)
    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO conversations(doc_id, pages, messages) VALUES(?,?,?)",
        (body.doc_id, json.dumps(body.pages), json.dumps(body.messages, ensure_ascii=False)),
    )
    conn.commit()
    return {"conv_id": cur.lastrowid}


@app.get("/api/conversations")
def list_conversations(doc_id: int, page: Optional[int] = None):
    """该课件的对话列表；带 page 时按 pages JSON 数组包含该页过滤（json_each）。"""
    conn = db.get_conn()
    if page is not None:
        rows = conn.execute(
            """SELECT * FROM conversations
               WHERE doc_id=? AND ? IN (SELECT value FROM json_each(pages))
               ORDER BY id DESC""",
            (doc_id, page),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE doc_id=? ORDER BY id DESC", (doc_id,)
        ).fetchall()
    return [
        {
            "id": r["id"],
            "doc_id": r["doc_id"],
            "pages": json.loads(r["pages"]),
            "messages": json.loads(r["messages"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: int):
    """删除一条存档对话（物理删除）；id 不存在 → 404。"""
    conn = db.get_conn()
    cur = conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, f"对话不存在：{conv_id}")
    return {"ok": True}


# ---------- model providers / configs（P2b 模型与 API 设置） ----------

class ModelProviderIn(BaseModel):
    name: str
    type: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    auth_type: str = "bearer"
    extra_headers: dict = {}
    enabled: bool = True
    note: str = ""


class ModelProviderPatch(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    auth_type: Optional[str] = None
    extra_headers: Optional[dict] = None
    enabled: Optional[bool] = None
    note: Optional[str] = None


class ModelConfigIn(BaseModel):
    provider_id: int
    model_name: str
    capability: Literal["chat", "vision", "image_gen"]
    enabled: bool = True
    priority: int = 100
    params: dict = {}
    note: str = ""


class ModelConfigPatch(BaseModel):
    provider_id: Optional[int] = None
    model_name: Optional[str] = None
    capability: Optional[Literal["chat", "vision", "image_gen"]] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None
    params: Optional[dict] = None
    note: Optional[str] = None


class ModelTestIn(BaseModel):
    capability: Literal["chat", "vision", "image_gen"]
    provider_id: int
    model_name: str
    sample: Optional[str] = None


def _json_dumps(obj: object) -> str:
    return json.dumps(obj or {}, ensure_ascii=False)


def _json_loads_dict(raw: str | None) -> dict:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _provider_row_dict(row: sqlite3.Row) -> dict:
    import providers

    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "base_url": row["base_url"],
        "auth_type": row["auth_type"],
        "extra_headers": providers.mask_headers(_json_loads_dict(row["extra_headers_json"])),
        "enabled": bool(row["enabled"]),
        "note": row["note"],
        "masked_key": providers.mask_key(row["api_key_ciphertext"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _config_row_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "provider_id": row["provider_id"],
        "provider_name": row["provider_name"] if "provider_name" in row.keys() else None,
        "model_name": row["model_name"],
        "capability": row["capability"],
        "enabled": bool(row["enabled"]),
        "priority": row["priority"],
        "params": _json_loads_dict(row["params_json"]),
        "note": row["note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _get_model_provider(provider_id: int) -> sqlite3.Row:
    row = db.get_conn().execute("SELECT * FROM model_providers WHERE id=?", (provider_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"model provider {provider_id} 不存在")
    return row


def _get_model_config(config_id: int) -> sqlite3.Row:
    row = db.get_conn().execute("SELECT * FROM model_configs WHERE id=?", (config_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"model config {config_id} 不存在")
    return row


@app.get("/api/model-providers")
def list_model_providers():
    rows = db.get_conn().execute("SELECT * FROM model_providers ORDER BY id DESC").fetchall()
    return [_provider_row_dict(r) for r in rows]


@app.post("/api/model-providers")
def create_model_provider(body: ModelProviderIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Provider 名称不能为空")
    conn = db.get_conn()
    cur = conn.execute(
        """INSERT INTO model_providers(name,type,base_url,api_key_ciphertext,auth_type,extra_headers_json,enabled,note)
           VALUES(?,?,?,?,?,?,?,?)""",
        (name, body.type, body.base_url.rstrip("/"), body.api_key, body.auth_type,
         _json_dumps(body.extra_headers), 1 if body.enabled else 0, body.note),
    )
    conn.commit()
    return {"provider_id": cur.lastrowid}


@app.patch("/api/model-providers/{provider_id}")
def patch_model_provider(provider_id: int, body: ModelProviderPatch):
    _get_model_provider(provider_id)
    fields: list[str] = []
    params: list[object] = []
    for attr, col in (("name", "name"), ("type", "type"), ("auth_type", "auth_type"), ("note", "note")):
        if attr in body.model_fields_set and getattr(body, attr) is not None:
            val = getattr(body, attr).strip() if isinstance(getattr(body, attr), str) else getattr(body, attr)
            fields.append(f"{col}=?")
            params.append(val)
    if "base_url" in body.model_fields_set and body.base_url is not None:
        fields.append("base_url=?")
        params.append(body.base_url.rstrip("/"))
    if "api_key" in body.model_fields_set and body.api_key:
        fields.append("api_key_ciphertext=?")
        params.append(body.api_key)
    if "extra_headers" in body.model_fields_set and body.extra_headers is not None:
        import providers
        fields.append("extra_headers_json=?")
        current = _json_loads_dict(_get_model_provider(provider_id)["extra_headers_json"])
        params.append(_json_dumps(providers.merge_masked_headers(current, body.extra_headers)))
    if "enabled" in body.model_fields_set and body.enabled is not None:
        fields.append("enabled=?")
        params.append(1 if body.enabled else 0)
    if fields:
        fields.append("updated_at=datetime('now','localtime')")
        conn = db.get_conn()
        conn.execute(f"UPDATE model_providers SET {', '.join(fields)} WHERE id=?", (*params, provider_id))
        conn.commit()
    return _provider_row_dict(_get_model_provider(provider_id))


@app.delete("/api/model-providers/{provider_id}")
def delete_model_provider(provider_id: int):
    _get_model_provider(provider_id)
    conn = db.get_conn()
    n = conn.execute("SELECT COUNT(*) AS n FROM model_configs WHERE provider_id=?", (provider_id,)).fetchone()["n"]
    if n:
        raise HTTPException(409, {"msg": "Provider 已被模型配置引用，先删除/迁移模型配置", "model_config_count": n})
    conn.execute("DELETE FROM model_providers WHERE id=?", (provider_id,))
    conn.commit()
    return {"ok": True}


@app.get("/api/model-configs")
def list_model_configs(capability: Optional[Literal["chat", "vision", "image_gen"]] = None):
    conn = db.get_conn()
    if capability:
        rows = conn.execute(
            """SELECT mc.*, mp.name AS provider_name FROM model_configs mc
               JOIN model_providers mp ON mp.id = mc.provider_id
               WHERE mc.capability=? ORDER BY mc.priority ASC, mc.id ASC""",
            (capability,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT mc.*, mp.name AS provider_name FROM model_configs mc
               JOIN model_providers mp ON mp.id = mc.provider_id
               ORDER BY mc.capability, mc.priority ASC, mc.id ASC"""
        ).fetchall()
    return [_config_row_dict(r) for r in rows]


@app.post("/api/model-configs")
def create_model_config(body: ModelConfigIn):
    _get_model_provider(body.provider_id)
    if not body.model_name.strip():
        raise HTTPException(422, "模型名不能为空")
    conn = db.get_conn()
    cur = conn.execute(
        """INSERT INTO model_configs(provider_id,model_name,capability,enabled,priority,params_json,note)
           VALUES(?,?,?,?,?,?,?)""",
        (body.provider_id, body.model_name.strip(), body.capability, 1 if body.enabled else 0,
         body.priority, _json_dumps(body.params), body.note),
    )
    conn.commit()
    return {"model_config_id": cur.lastrowid}


@app.patch("/api/model-configs/{config_id}")
def patch_model_config(config_id: int, body: ModelConfigPatch):
    _get_model_config(config_id)
    fields: list[str] = []
    params: list[object] = []
    if body.provider_id is not None:
        _get_model_provider(body.provider_id)
        fields.append("provider_id=?")
        params.append(body.provider_id)
    for attr in ("model_name", "capability", "priority", "note"):
        if attr in body.model_fields_set and getattr(body, attr) is not None:
            val = getattr(body, attr).strip() if attr == "model_name" else getattr(body, attr)
            fields.append(f"{attr}=?")
            params.append(val)
    if "enabled" in body.model_fields_set and body.enabled is not None:
        fields.append("enabled=?")
        params.append(1 if body.enabled else 0)
    if "params" in body.model_fields_set and body.params is not None:
        fields.append("params_json=?")
        params.append(_json_dumps(body.params))
    if fields:
        fields.append("updated_at=datetime('now','localtime')")
        conn = db.get_conn()
        conn.execute(f"UPDATE model_configs SET {', '.join(fields)} WHERE id=?", (*params, config_id))
        conn.commit()
    return _config_row_dict(_get_model_config(config_id))


@app.delete("/api/model-configs/{config_id}")
def delete_model_config(config_id: int):
    _get_model_config(config_id)
    conn = db.get_conn()
    conn.execute("DELETE FROM model_configs WHERE id=?", (config_id,))
    conn.commit()
    return {"ok": True}


@app.post("/api/model-configs/test")
async def test_model_config(body: ModelTestIn):
    import providers

    return await providers.test_model_connection(body.capability, body.provider_id, body.model_name, body.sample)


@app.get("/api/health")
def health():
    import providers

    return {"ok": True, "providers": providers.describe()}
