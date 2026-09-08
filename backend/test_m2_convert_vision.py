"""M2 单元测试：convert 路径 + vision 拼装/回填逻辑（LLM mock，不打真实网关）。

运行（项目根，macOS/Linux）：
    backend/venv/bin/python -m unittest discover -s backend -p "test_m2*.py" -t .
或 backend/ 下：
    ../backend/venv/bin/python -m unittest test_m2_convert_vision
Windows：
    backend\\venv\\Scripts\\python -m unittest test_m2_convert_vision（backend/ 下）
"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import vision  # noqa: E402
from convert import find_soffice  # noqa: E402


class TestConvertSofficeDetect(unittest.TestCase):
    """soffice 探测：环境上装没装 LibreOffice 都能给出确定结果。"""

    def test_find_soffice_returns_path_or_none(self):
        result = find_soffice()
        # 装了 LibreOffice → 返回命中路径（默认安装位或 PATH 中的任一处）；没装 → None
        if result is not None:
            self.assertTrue(Path(result).is_file(), f"探测结果不是文件：{result}")
        else:
            # 没装时不应有任何默认安装位命中（候选列表逐项校验）
            import convert as convert_mod

            for cand in convert_mod.SOFFICE_CANDIDATES:
                self.assertFalse(
                    Path(cand).is_file(),
                    f"候选 {cand} 存在但 find_soffice 未命中（探测逻辑有 bug）",
                )
            for name in ("soffice", "soffice.exe", "soffice.com"):
                self.assertIsNone(shutil.which(name), f"PATH 中存在 {name} 但未命中")

    def test_soffice_candidates_cover_platforms(self):
        """候选路径覆盖三平台常见安装位（静态检查，与是否安装无关）。"""
        import convert as convert_mod

        joined = "\n".join(convert_mod.SOFFICE_CANDIDATES)
        # macOS cask / Linux 包管理器 / Windows Program Files 三类默认位都要在
        self.assertIn("/Applications/LibreOffice.app", joined)
        self.assertIn("/usr/bin/soffice", joined)
        if sys.platform.startswith("win"):
            self.assertIn("LibreOffice", joined)
            self.assertIn("soffice.exe", joined)

    def test_install_hint_matches_platform(self):
        """报错提示按平台给出对应安装命令。"""
        from convert import _soffice_install_hint

        hint = _soffice_install_hint()
        self.assertTrue(hint, "安装提示不应为空")
        if sys.platform.startswith("win"):
            self.assertIn("Windows", hint)
        elif sys.platform == "darwin":
            self.assertIn("macOS", hint)
            self.assertIn("brew", hint)
        else:
            self.assertIn("Linux", hint)

    def test_missing_src_raises(self):
        from convert import convert_to_pdf

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(RuntimeError) as ctx:
                convert_to_pdf(Path(td) / "nope.pptx", td, timeout_s=5)
            self.assertIn("不存在", str(ctx.exception))


class TestVisionMessageAssembly(unittest.TestCase):
    """消息拼装：base64 图走 image_url、prompt 含 LaTeX 转写要求。"""

    def test_build_vision_messages_structure(self):
        import base64

        png_b64 = base64.b64encode(b"\x89PNG-fake").decode()
        msgs = vision.build_vision_messages(png_b64)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "user")
        parts = msgs[0]["content"]
        self.assertEqual(parts[0]["type"], "text")
        self.assertIn("LaTeX", parts[0]["text"])
        self.assertEqual(parts[1]["type"], "image_url")
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertIn(png_b64, parts[1]["image_url"]["url"])


class TestTranscribePages(unittest.TestCase):
    """transcribe_pages 回填逻辑（render/read 全 mock，不碰网关与磁盘 PDF）。"""

    def _setup_db(self, pages: dict[int, str]) -> int:
        import db as dbm

        dbm.DB_PATH = Path(self.tmpdir) / "test.db"
        dbm._local = __import__("threading").local()  # 重置线程连接缓存
        dbm.init_db()
        conn = dbm.get_conn()
        conn.execute("INSERT INTO subjects(name) VALUES('测试科目')")
        conn.execute("INSERT INTO documents(subject_id, folder_id, filename, orig_path, pdf_path)"
                     " VALUES(1, NULL, 't.pptx', 'o', 'p')")
        doc_id = conn.execute("SELECT MAX(id) m FROM documents").fetchone()["m"]
        for no, text in pages.items():
            conn.execute(
                "INSERT INTO document_pages(doc_id, page_no, text, lang, source, width, height)"
                " VALUES(?,?,?,?,?,?,?)", (doc_id, no, text, "zh", "text", 720, 405))
        conn.commit()
        return doc_id

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_backfill_success_and_lang_redetect(self):
        import db as dbm

        doc_id = self._setup_db({1: "有文本", 2: "", 3: ""})

        calls = []

        async def fake_read(png: bytes) -> str:
            calls.append(len(png))
            return "The quick brown fox jumps over the lazy dog dogs"  # 英文 → lang 重检测为 en

        def fake_render(pdf_path, page_no, dpi=150):
            return b"PNG" + bytes([page_no])

        v, f = asyncio.run(vision.transcribe_pages(
            doc_id, "fake.pdf", [2, 3], read_page=fake_read, render_page=fake_render))
        self.assertEqual(v, [2, 3])
        self.assertEqual(f, [])
        self.assertEqual(len(calls), 2)
        row = dbm.get_conn().execute(
            "SELECT text, lang, source FROM document_pages WHERE doc_id=? AND page_no=2",
            (doc_id,)).fetchone()
        self.assertEqual(row["source"], "vision")
        self.assertEqual(row["lang"], "en")
        # 非空文本页不应被改动
        row1 = dbm.get_conn().execute(
            "SELECT source FROM document_pages WHERE doc_id=? AND page_no=1",
            (doc_id,)).fetchone()
        self.assertEqual(row1["source"], "text")

    def test_single_page_failure_degrades_silently(self):
        import db as dbm

        doc_id = self._setup_db({1: "", 2: "", 3: ""})

        async def flaky_read(png: bytes) -> str:
            if b"\x02" in png:  # 第 2 页失败（OpenAI-compatible 抖动模拟）
                raise RuntimeError("gateway 502")
            return "看图转写：极限的定义 lim"

        def fake_render(pdf_path, page_no, dpi=150):
            return b"PNG" + bytes([page_no])

        v, f = asyncio.run(vision.transcribe_pages(
            doc_id, "fake.pdf", [1, 2, 3], read_page=flaky_read, render_page=fake_render))
        self.assertEqual(v, [1, 3])
        self.assertEqual(f, [2])
        row2 = dbm.get_conn().execute(
            "SELECT text, source FROM document_pages WHERE doc_id=? AND page_no=2",
            (doc_id,)).fetchone()
        self.assertEqual(row2["text"], "")
        self.assertEqual(row2["source"], "text")  # 失败页不改 source

    def test_empty_transcription_counts_as_failure(self):
        doc_id = self._setup_db({1: ""})

        async def blank_read(png: bytes) -> str:
            return "   "  # 空白输出 → 视为失败，保持空页

        def fake_render(pdf_path, page_no, dpi=150):
            return b"PNG"

        v, f = asyncio.run(vision.transcribe_pages(
            doc_id, "fake.pdf", [1], read_page=blank_read, render_page=fake_render))
        self.assertEqual(v, [])
        self.assertEqual(f, [1])

    def test_page_limit_is_ignored_after_async_pipeline(self):
        doc_id = self._setup_db({i: "" for i in range(1, 6)})  # 5 空页

        async def ok_read(png: bytes) -> str:
            return "文本"

        def fake_render(pdf_path, page_no, dpi=150):
            return b"PNG"

        v, f = asyncio.run(vision.transcribe_pages(
            doc_id, "fake.pdf", [1, 2, 3, 4, 5], page_limit=3,
            read_page=ok_read, render_page=fake_render))
        self.assertEqual(v, [1, 2, 3, 4, 5])
        self.assertEqual(f, [])  # 异步主链路不再因上传阻塞设置页数上限


if __name__ == "__main__":
    unittest.main()
