"""fig_desc 上传后后台预跑单测（mock vision，不触网）。"""
import asyncio
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi import UploadFile
from fastapi.testclient import TestClient

import db
import main
import vision
from extract import PageData


class FigPrerunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / "test.db"
        db.DATA_DIR = Path(self.tmp.name)
        db._local = threading.local()
        db.init_db()
        self.conn = db.get_conn()
        self.conn.execute("INSERT INTO subjects(id,name,sort_order) VALUES(1,'测试科目',0)")
        self.conn.commit()
        self._old_originals = main.ORIGINALS_DIR
        self._old_pdfs = main.PDFS_DIR
        main.ORIGINALS_DIR = Path(self.tmp.name) / "originals"
        main.PDFS_DIR = Path(self.tmp.name) / "pdfs"
        main.ORIGINALS_DIR.mkdir(parents=True, exist_ok=True)
        main.PDFS_DIR.mkdir(parents=True, exist_ok=True)
        self._reset_queue_state()

    def tearDown(self):
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
            del db._local.conn
        main.ORIGINALS_DIR = self._old_originals
        main.PDFS_DIR = self._old_pdfs
        self._reset_queue_state()
        self.tmp.cleanup()

    def _reset_queue_state(self):
        vision._queue = None
        vision._worker_task = None
        vision._background_tasks.clear()
        vision._queued_doc_ids.clear()
        vision._queued_fig_doc_ids.clear()
        vision._running_doc_id = None
        vision._running_job = None

    def _doc(self, *, n_pages: int = 3, has_image_pages=None, cached_pages=None) -> int:
        has_image_pages = set(has_image_pages or [])
        cached_pages = set(cached_pages or [])
        cur = self.conn.execute(
            """INSERT INTO documents(subject_id, folder_id, filename, orig_path, pdf_path,
                                      page_count, sort_order)
               VALUES(1, NULL, 't.pdf', 'o.pdf', 'p.pdf', ?, 0)""",
            (n_pages,),
        )
        doc_id = int(cur.lastrowid)
        for no in range(1, n_pages + 1):
            fig_desc = f"cached {no}" if no in cached_pages else None
            self.conn.execute(
                """INSERT INTO document_pages(doc_id,page_no,text,lang,source,width,height,has_image,fig_desc)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (doc_id, no, f"P{no}", "zh", "text", 720, 405,
                 1 if no in has_image_pages else 0, fig_desc),
            )
        self.conn.commit()
        return doc_id

    def test_upload_enqueues_fig_prerun_after_insert(self):
        pages = [
            PageData(page_no=1, text="正文1", lang="zh", width=720, height=405, has_image=1),
            PageData(page_no=2, text="正文2", lang="zh", width=720, height=405, has_image=0),
        ]

        async def run_upload():
            upload_file = UploadFile(file=io.BytesIO(b"%PDF-1.4 fake"), filename="x.pdf")
            with mock.patch.object(main, "extract_pdf", return_value=pages):
                resp = await main.upload(subject_id=1, folder_id=None, file=upload_file)
                # 让 create_task 包装的 enqueue_fig_prerun_async 执行完。
                await asyncio.sleep(0)
                return resp

        resp = asyncio.run(run_upload())
        self.assertEqual(resp["doc_id"], 1)
        self.assertEqual(vision._queued_fig_doc_ids, {1})
        self.assertEqual(vision._queue.qsize(), 1)

    def test_fig_prerun_limit_40_pages(self):
        doc_id = self._doc(n_pages=45, has_image_pages=range(1, 46))

        async def fake_describe(png: bytes) -> str:
            return f"desc {png.decode()}"

        def fake_render(_pdf, page_no, dpi=150):
            return str(page_no).encode()

        asyncio.run(vision.run_fig_prerun_job(
            doc_id, describe_page=fake_describe, render_page=fake_render
        ))
        rows = self.conn.execute(
            "SELECT page_no, fig_desc FROM document_pages WHERE doc_id=? ORDER BY page_no",
            (doc_id,),
        ).fetchall()
        described = [r["page_no"] for r in rows if r["fig_desc"] is not None]
        pending = [r["page_no"] for r in rows if r["fig_desc"] is None]
        self.assertEqual(described, list(range(1, 41)))
        self.assertEqual(pending, list(range(41, 46)))

    def test_enqueue_fig_prerun_dedupes_same_doc(self):
        doc_id = self._doc(n_pages=1, has_image_pages=[1])
        self.assertTrue(vision.enqueue_fig_prerun(doc_id))
        self.assertFalse(vision.enqueue_fig_prerun(doc_id))
        self.assertEqual(vision._queued_fig_doc_ids, {doc_id})
        self.assertEqual(vision._queue.qsize(), 1)
        vision._queued_fig_doc_ids.clear()
        vision._running_job = (vision.JOB_FIG_PRERUN, doc_id)
        self.assertFalse(vision.enqueue_fig_prerun(doc_id))

    def test_startup_recover_scans_pending_fig_preruns(self):
        d1 = self._doc(n_pages=2, has_image_pages=[1])
        d2 = self._doc(n_pages=2, has_image_pages=[1], cached_pages=[1])
        d3 = self._doc(n_pages=2, has_image_pages=[2])
        recovered = vision.recover_pending_fig_preruns()
        self.assertEqual(recovered, [d1, d3])
        self.assertEqual(vision._queued_fig_doc_ids, {d1, d3})
        self.assertNotIn(d2, vision._queued_fig_doc_ids)

    def test_failed_page_keeps_null_and_others_save(self):
        doc_id = self._doc(n_pages=3, has_image_pages=[1, 2, 3])

        async def fake_describe(png: bytes) -> str:
            if png == b"2":
                raise RuntimeError("vision 503")
            return f"desc {png.decode()}"

        def fake_render(_pdf, page_no, dpi=150):
            return str(page_no).encode()

        asyncio.run(vision.run_fig_prerun_job(
            doc_id, describe_page=fake_describe, render_page=fake_render
        ))
        rows = {
            r["page_no"]: r["fig_desc"]
            for r in self.conn.execute(
                "SELECT page_no, fig_desc FROM document_pages WHERE doc_id=?", (doc_id,)
            ).fetchall()
        }
        self.assertEqual(rows[1], "desc 1")
        self.assertIsNone(rows[2])
        self.assertEqual(rows[3], "desc 3")

    def test_force_clears_cache_and_requeues(self):
        doc_id = self._doc(n_pages=2, has_image_pages=[1, 2], cached_pages=[1, 2])
        resp = asyncio.run(main.fig_prerun(doc_id, main.FigPrerunIn(force=True)))
        self.assertTrue(resp["queued"])
        self.assertTrue(resp["force"])
        self.assertEqual(resp["pending_pages"], 2)
        rows = self.conn.execute(
            "SELECT fig_desc FROM document_pages WHERE doc_id=? ORDER BY page_no", (doc_id,)
        ).fetchall()
        self.assertEqual([r["fig_desc"] for r in rows], [None, None])

    def test_manual_endpoint_200_and_404(self):
        doc_id = self._doc(n_pages=1, has_image_pages=[1])
        with TestClient(main.app) as client:
            ok = client.post(f"/api/docs/{doc_id}/fig/prerun", json={})
            missing = client.post("/api/docs/999/fig/prerun", json={})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["doc_id"], doc_id)
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
