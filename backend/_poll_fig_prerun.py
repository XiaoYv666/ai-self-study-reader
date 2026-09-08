"""临时 E2E：轮询 fig_desc 预跑进度。"""
import sqlite3
import time
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "app.db"
start = time.monotonic()
for i in range(11):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT doc_id,
                  SUM(CASE WHEN has_image=1 THEN 1 ELSE 0 END) AS has_img,
                  SUM(CASE WHEN has_image=1 AND fig_desc IS NOT NULL THEN 1 ELSE 0 END) AS cached,
                  SUM(CASE WHEN has_image=1 AND fig_desc IS NULL THEN 1 ELSE 0 END) AS pending
           FROM document_pages
           WHERE doc_id IN (14,15)
           GROUP BY doc_id
           ORDER BY doc_id"""
    ).fetchall()
    print(f"t={time.monotonic()-start:.1f}s", [dict(r) for r in rows], flush=True)
    conn.close()
    if i < 10:
        time.sleep(30)
