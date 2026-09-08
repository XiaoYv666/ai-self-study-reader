"""异步 vision 管线单测：入队、进度、失败重试、重启恢复（LLM/PDF 全 mock）。"""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

import db
import vision


class AsyncVisionPipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmpdir.name) / "test.db"
        db._local = threading.local()
        db.init_db()
        self.conn = db.get_conn()
        self.conn.execute("INSERT INTO subjects(name) VALUES('测试科目')")

    def tearDown(self):
        self.tmpdir.cleanup()
        # 清理模块级队列状态，避免用例互相影响。
        vision._queue = None
        vision._worker_task = None
        vision._background_tasks.clear()
        vision._queued_doc_ids.clear()
        vision._running_doc_id = None

    def _doc(self, pages: dict[int, str], status: str = "done") -> int:
        cur = self.conn.execute(
            """INSERT INTO documents(subject_id, folder_id, filename, orig_path, pdf_path,
                                      page_count, vision_status, vision_done, vision_total, failed_pages)
               VALUES(1, NULL, 't.pdf', 'o', 'p.pdf', ?, ?, 0, 0, '[]')""",
            (len(pages), status),
        )
        doc_id = int(cur.lastrowid)
        for no, text in pages.items():
            self.conn.execute(
                "INSERT INTO document_pages(doc_id, page_no, text, lang, source, width, height)"
                " VALUES(?,?,?,?,?,?,?)",
                (doc_id, no, text, "zh", "text", 720, 405),
            )
        self.conn.commit()
        return doc_id

    def test_enqueue_dedupes_and_records_pending(self):
        doc_id = self._doc({1: "", 2: ""})
        vision.init_doc_vision_job(doc_id, 2)
        self.assertTrue(vision.enqueue_vision_doc(doc_id))
        self.assertFalse(vision.enqueue_vision_doc(doc_id))
        status = vision.get_doc_vision_status(doc_id)
        self.assertEqual(status, {"status": "pending", "done": 0, "total": 2, "failed_pages": []})

    def test_progress_persists_and_marks_done(self):
        doc_id = self._doc({1: "", 2: ""}, status="pending")
        vision.init_doc_vision_job(doc_id, 2)

        async def fake_read(png: bytes) -> str:
            await asyncio.sleep(0)
            return f"识别文本 {png.decode()}"

        def fake_render(_pdf, page_no, dpi=150):
            return str(page_no).encode()

        asyncio.run(vision.run_document_job(doc_id, read_page=fake_read, render_page=fake_render, retry_delay_s=0))
        self.assertEqual(vision.get_doc_vision_status(doc_id), {"status": "done", "done": 2, "total": 2, "failed_pages": []})
        rows = self.conn.execute(
            "SELECT page_no, source FROM document_pages WHERE doc_id=? ORDER BY page_no", (doc_id,)
        ).fetchall()
        self.assertEqual([(r["page_no"], r["source"]) for r in rows], [(1, "vision"), (2, "vision")])

    def test_failure_retries_once_then_failed_page_partial(self):
        doc_id = self._doc({1: "", 2: ""}, status="pending")
        vision.init_doc_vision_job(doc_id, 2)
        attempts: dict[int, int] = {}

        async def fake_read(png: bytes) -> str:
            page_no = int(png.decode())
            attempts[page_no] = attempts.get(page_no, 0) + 1
            if page_no == 2:
                raise RuntimeError("gateway 503")
            return "第一页识别成功"

        def fake_render(_pdf, page_no, dpi=150):
            return str(page_no).encode()

        asyncio.run(vision.run_document_job(doc_id, read_page=fake_read, render_page=fake_render, retry_delay_s=0))
        self.assertEqual(attempts[2], 2)
        self.assertEqual(vision.get_doc_vision_status(doc_id), {"status": "partial", "done": 2, "total": 2, "failed_pages": [2]})
        row = self.conn.execute(
            "SELECT text, source FROM document_pages WHERE doc_id=? AND page_no=2", (doc_id,)
        ).fetchone()
        self.assertEqual((row["text"], row["source"]), ("", "text"))

    def test_recover_pending_jobs_enqueues_pending_and_running(self):
        d1 = self._doc({1: ""}, status="pending")
        d2 = self._doc({1: ""}, status="running")
        self._doc({1: ""}, status="done")
        recovered = vision.recover_pending_jobs()
        self.assertEqual(recovered, [d1, d2])
        self.assertEqual(vision._queued_doc_ids, {d1, d2})


if __name__ == "__main__":
    unittest.main()
