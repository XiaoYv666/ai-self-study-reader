"""SQLite 建表与访问层。

数据模型按《技术文档-方案.md》§4：
subjects / folders / documents / document_pages / conversations
启动时自动建表（CREATE TABLE IF NOT EXISTS），零运维。
"""
import sqlite3
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "app.db"

# SQLite 在多线程 FastAPI 下用「每线程一个连接 + check_same_thread=False」的本地线程池方案
_local = threading.local()


def get_conn() -> sqlite3.Connection:
    """当前线程的数据库连接（惰性建立）。row_factory 使查询结果可按列名取值。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")  # 读写并发更友好
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS subjects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS folders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id INTEGER NOT NULL REFERENCES subjects(id),
    parent_id  INTEGER REFERENCES folders(id),
    name       TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS documents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id INTEGER NOT NULL REFERENCES subjects(id),
    folder_id  INTEGER REFERENCES folders(id),
    filename   TEXT NOT NULL,
    orig_path  TEXT NOT NULL,
    pdf_path   TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0,
    vision_status TEXT NOT NULL DEFAULT 'done', -- pending | running | done | partial
    vision_done INTEGER NOT NULL DEFAULT 0,
    vision_total INTEGER NOT NULL DEFAULT 0,
    failed_pages TEXT NOT NULL DEFAULT '[]',     -- JSON array[int]
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS document_pages (
    doc_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no INTEGER NOT NULL,
    text    TEXT NOT NULL DEFAULT '',
    lang    TEXT NOT NULL DEFAULT 'zh',   -- 'zh' | 'en'，上传时检测写入
    source  TEXT NOT NULL DEFAULT 'text', -- 'text'（原生文本层）| 'vision'（读图回填，M2）
    width   REAL,                          -- 页面实际宽（pt），PyMuPDF 读取
    height  REAL,                          -- 页面实际高（pt）
    fig_desc TEXT,                         -- 页面图形描述（AI 识图，chat 按需生成缓存；NULL=未识读）
    has_image INTEGER DEFAULT NULL,        -- 页内嵌入图片物理检测（上传时 PyMuPDF 写入）：NULL=未检测；1=有内容图；0=无
    PRIMARY KEY (doc_id, page_no)
);

CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id     INTEGER NOT NULL REFERENCES documents(id),
    pages      TEXT NOT NULL,   -- JSON 数组，如 [12,13]
    messages   TEXT NOT NULL,   -- JSON：完整对话记录
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS model_providers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    type                TEXT NOT NULL DEFAULT 'openai_compatible',
    base_url            TEXT NOT NULL DEFAULT '',
    api_key_ciphertext  TEXT NOT NULL DEFAULT '',
    auth_type           TEXT NOT NULL DEFAULT 'bearer',
    extra_headers_json  TEXT NOT NULL DEFAULT '{}',
    enabled             INTEGER NOT NULL DEFAULT 1,
    note                TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS model_configs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL REFERENCES model_providers(id),
    model_name  TEXT NOT NULL,
    capability  TEXT NOT NULL CHECK (capability IN ('chat','vision','image_gen')),
    enabled     INTEGER NOT NULL DEFAULT 1,
    priority    INTEGER NOT NULL DEFAULT 100,
    params_json TEXT NOT NULL DEFAULT '{}',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS app_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_folders_subject  ON folders(subject_id);
CREATE INDEX IF NOT EXISTS idx_docs_subject     ON documents(subject_id);
CREATE INDEX IF NOT EXISTS idx_docs_folder      ON documents(folder_id);
CREATE INDEX IF NOT EXISTS idx_conv_doc         ON conversations(doc_id);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """轻量迁移：SQLite CREATE TABLE IF NOT EXISTS 不会补既有列。"""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db() -> None:
    """建表 + 轻量迁移（幂等）。FastAPI 启动时调用。"""
    conn = get_conn()
    conn.executescript(SCHEMA)
    _ensure_column(conn, "documents", "vision_status", "vision_status TEXT NOT NULL DEFAULT 'done'")
    _ensure_column(conn, "documents", "vision_done", "vision_done INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "documents", "vision_total", "vision_total INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "documents", "failed_pages", "failed_pages TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "document_pages", "fig_desc", "fig_desc TEXT")  # 图形按需识读缓存
    # 页内嵌入图片物理检测（chat 有图直接识图触发用；NULL=未检测，启动时后台补跑回填）
    _ensure_column(conn, "document_pages", "has_image", "has_image INTEGER DEFAULT NULL")
    conn.commit()


def next_sort_order(table: str, where: str = "", params: tuple = ()) -> int:
    """取排序位：同容器内 max(sort_order)+1，空表返回 0。table 仅限白名单调用点。"""
    assert table in ("subjects", "folders", "documents")
    conn = get_conn()
    row = conn.execute(
        f"SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM {table} {where}", params
    ).fetchone()
    return int(row["n"])


def next_child_sort_order(subject_id: int, parent_id: int | None) -> int:
    """文件夹和课件在同一父容器内共享排序位。"""
    conn = get_conn()
    row = conn.execute(
        """SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM (
               SELECT sort_order FROM folders WHERE subject_id=? AND parent_id IS ?
               UNION ALL
               SELECT sort_order FROM documents WHERE subject_id=? AND folder_id IS ?
           )""",
        (subject_id, parent_id, subject_id, parent_id),
    ).fetchone()
    return int(row["n"])
