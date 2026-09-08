"""图形按需识读单测（mock vision 调用，不触网）。

覆盖：
- 关键词命中触发 / 不触发（FIGURE_KEYWORD_RE + chat 路由 gating）
- fig_desc 缓存后二次 chat 不再调 vision
- fig_desc 注入 page context 格式（【图中内容（AI 识图）】块）
- describe 失败不阻塞 chat（fig_desc 保持 NULL，SSE 正常）
- 迁移幂等（init_db 两次 + 旧库补列）
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import main
import prompts
import vision


async def _iter_events(chunk):
    for block in chunk.split("\n\n"):
        if block.strip():
            lines = block.split("\n")
            yield (lines[0][len("event: "):], json.loads(lines[1][len("data: "):]))


async def _drain(resp):
    """收集 SSE StreamingResponse 的全部事件。"""
    events = []
    async for chunk in resp.body_iterator:
        async for e in _iter_events(chunk):
            events.append(e)
    return events


def _make_pdf(path: Path) -> str:
    """生成真实可渲染的 1 页 PDF（render_page_png 实际路径用）。"""
    import pymupdf

    doc = pymupdf.open()
    doc.new_page()
    doc.new_page()  # 第 2 页（describe_doc_figures 批量渲染需要）
    doc.save(str(path))
    doc.close()
    return str(path)


class FigDescBase(unittest.TestCase):
    """公共夹具：临时库 + 一份课件（2 页，fig_desc 均 NULL）。"""

    def setUp(self):
        old = getattr(db._local, "conn", None)
        if old is not None:
            old.close()
            del db._local.conn
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / "test.db"
        db.DATA_DIR = Path(self.tmp.name)
        db.init_db()
        conn = db.get_conn()
        conn.execute("INSERT INTO subjects(id,name,sort_order) VALUES(1,'电路',0)")
        # pdf_path 指向真实可渲染 PDF（chat 路由 os.path.exists + render_page_png 都会走到）
        self.pdf = _make_pdf(Path(self.tmp.name) / "fake.pdf")
        conn.execute(
            "INSERT INTO documents(id,subject_id,folder_id,filename,orig_path,pdf_path,page_count,sort_order)"
            " VALUES(1,1,NULL,'L09.pdf','o',?,2,0)",
            (self.pdf,),
        )
        for no in (1, 2):
            conn.execute(
                "INSERT INTO document_pages(doc_id,page_no,text,lang,source,width,height)"
                " VALUES(1,?,?,'zh','text',720,405)",
                (no, f"第{no}页正文内容"),
            )
        conn.commit()
        self._old_images_dir = main.IMAGES_DIR
        main.IMAGES_DIR = Path(self.tmp.name) / "images"
        main.IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
            del db._local.conn
        main.IMAGES_DIR = self._old_images_dir
        self.tmp.cleanup()


class MigrationTests(FigDescBase):
    def test_fig_desc_column_exists_and_idempotent(self):
        cols = {r["name"] for r in db.get_conn().execute("PRAGMA table_info(document_pages)")}
        self.assertIn("fig_desc", cols)
        db.init_db()  # 幂等：再跑一次不炸
        cols2 = {r["name"] for r in db.get_conn().execute("PRAGMA table_info(document_pages)")}
        self.assertIn("fig_desc", cols2)

    def test_migration_adds_column_to_old_db(self):
        # 模拟旧库：重建一张没有 fig_desc 的 document_pages → init_db 补列
        conn = db.get_conn()
        conn.execute("DROP TABLE document_pages")
        conn.execute(
            """CREATE TABLE document_pages (
                doc_id INTEGER NOT NULL, page_no INTEGER NOT NULL,
                text TEXT NOT NULL DEFAULT '', lang TEXT NOT NULL DEFAULT 'zh',
                source TEXT NOT NULL DEFAULT 'text', width REAL, height REAL,
                PRIMARY KEY (doc_id, page_no))"""
        )
        conn.commit()
        cols_before = {r["name"] for r in conn.execute("PRAGMA table_info(document_pages)")}
        self.assertNotIn("fig_desc", cols_before)
        db.init_db()
        cols_after = {r["name"] for r in db.get_conn().execute("PRAGMA table_info(document_pages)")}
        self.assertIn("fig_desc", cols_after)


class KeywordTests(unittest.TestCase):
    def test_keywords_match(self):
        for q in ("这页的图里画的是什么", "解释一下原理图", "电路怎么连", "波形周期多少",
                  "曲线在哪里相交", "示意图看不懂", "what is in the figure",
                  "describe the diagram", "analyze the circuit", "waveform timing",
                  "the graph shows", "plot these points", "WHAT IS THE FIGURE"):
            self.assertTrue(main.FIGURE_KEYWORD_RE.search(q), q)

    def test_keywords_miss(self):
        for q in ("这页讲的排序算法", "什么是数组", "总结本章内容", "help me study"):
            self.assertFalse(main.FIGURE_KEYWORD_RE.search(q), q)


class PageContextTests(unittest.TestCase):
    def test_fig_desc_injected_after_text(self):
        pages = [{
            "page_no": 3, "text": "Sorting Array 内容", "lang": "zh",
            "fig_desc": "下图：冒泡排序流程图 (flowchart)，两个嵌套循环节点。",
        }]
        ctx = prompts.build_page_context(pages)
        self.assertIn("【课件内容 P3】", ctx)
        self.assertIn("Sorting Array 内容", ctx)
        self.assertIn("【图中内容（AI 识图）】\n下图：冒泡排序流程图", ctx)
        self.assertLess(ctx.index("Sorting Array"), ctx.index("【图中内容"))  # 正文在前

    def test_fig_desc_null_or_empty_not_injected(self):
        for fd in (None, "", "   "):
            ctx = prompts.build_page_context(
                [{"page_no": 1, "text": "正文", "lang": "zh", "fig_desc": fd}]
            )
            self.assertNotIn("【图中内容", ctx)

    def test_empty_page_with_fig_desc(self):
        # 无文本层页但有图描述 → 占位符 + 图描述块
        ctx = prompts.build_page_context(
            [{"page_no": 8, "text": "", "lang": "zh", "fig_desc": "整页为电路图：..."}]
        )
        self.assertIn("（本页无可提取文本，可能为扫描图/图片页）", ctx)
        self.assertIn("【图中内容（AI 识图）】", ctx)

    def test_missing_fig_desc_key_backward_compatible(self):
        ctx = prompts.build_page_context([{"page_no": 1, "text": "旧调用方", "lang": "zh"}])
        self.assertIn("旧调用方", ctx)
        self.assertNotIn("【图中内容", ctx)


class DescribeDocFiguresTests(FigDescBase):
    def test_describe_saves_cache_and_returns_pages(self):
        async def fake_call(png: bytes) -> str:
            return "RC 电路 (RC circuit)，R=1kΩ 串 C=100nF。"

        saved = asyncio.run(vision.describe_doc_figures(
            1, self.pdf, [1, 2], describe_page=fake_call, total_timeout_s=10,
        ))
        self.assertEqual(saved, [1, 2])
        rows = db.get_conn().execute(
            "SELECT page_no, fig_desc FROM document_pages WHERE doc_id=1 ORDER BY page_no"
        ).fetchall()
        self.assertIn("RC 电路", rows[0]["fig_desc"])
        self.assertIn("RC 电路", rows[1]["fig_desc"])

    def test_describe_normalizes_no_figure_marker(self):
        async def fake_call(png: bytes) -> str:
            return "无图形"

        saved = asyncio.run(vision.describe_doc_figures(
            1, self.pdf, [1], describe_page=fake_call, total_timeout_s=10,
        ))
        self.assertEqual(saved, [1])  # 空串也是有效缓存 → 保存
        row = db.get_conn().execute(
            "SELECT fig_desc FROM document_pages WHERE doc_id=1 AND page_no=1"
        ).fetchone()
        self.assertEqual(row["fig_desc"], "")

    def test_describe_total_timeout_cancels_pending(self):
        """总超时到点：未完成页放弃（fig_desc 保持 NULL），已完成页保留。"""
        import time

        async def slow(png: bytes) -> str:
            await asyncio.sleep(30)
            return "太慢"

        t0 = time.monotonic()
        saved = asyncio.run(vision.describe_doc_figures(
            1, self.pdf, [1], describe_page=slow, total_timeout_s=0.5,
        ))
        self.assertEqual(saved, [])
        self.assertLess(time.monotonic() - t0, 5)
        row = db.get_conn().execute(
            "SELECT fig_desc FROM document_pages WHERE doc_id=1 AND page_no=1"
        ).fetchone()
        self.assertIsNone(row["fig_desc"])

    def test_describe_failure_leaves_null_and_chat_not_blocked(self):
        """describe 抛异常：页 fig_desc 保持 NULL，chat SSE 正常收尾。"""
        calls = {"n": 0}

        async def boom(png: bytes) -> str:
            calls["n"] += 1
            raise RuntimeError("vision gateway 503")

        async def run_chat():
            async def fake_stream(messages, usage_out=None):
                yield "回答不受影响。"

            with mock.patch.object(main, "stream_chat", fake_stream), \
                 mock.patch.object(vision, "describe_page_figures", boom):
                resp = await main.chat(main.ChatIn(
                    doc_id=1, pages=[1], question="这页的图里画了什么电路", history=None))
                return await _drain(resp)

        events = asyncio.run(run_chat())
        self.assertEqual(calls["n"], 1)
        types = [t for t, _ in events]
        self.assertEqual(types[-1], "done")
        delta_text = "".join(d["content"] for t, d in events if t == "delta")
        self.assertEqual(delta_text, "回答不受影响。")
        row = db.get_conn().execute(
            "SELECT fig_desc FROM document_pages WHERE doc_id=1 AND page_no=1"
        ).fetchone()
        self.assertIsNone(row["fig_desc"])  # 未缓存，下次问图会再试


class ChatRouteTests(FigDescBase):
    def _fake_describe_doc(self, results: dict):
        """可直接 mock 到 vision.describe_doc_figures 的 async 假实现（写库+返回页码）。"""
        async def fake(doc_id, pdf_path, page_nos, **kw):
            for no in page_nos:
                if no in results:
                    vision.save_fig_desc(doc_id, no, results[no])
            return sorted(no for no in page_nos if no in results)
        return fake

    def test_keyword_hit_triggers_describe_and_injects(self):
        captured = {}

        async def fake_stream(messages, usage_out=None):
            captured["user"] = messages[-1]["content"]
            yield "看图作答。"

        fake = self._fake_describe_doc({1: "P1 图：二分查找流程图 (binary search flowchart)。"})

        async def run():
            with mock.patch.object(main, "stream_chat", fake_stream), \
                 mock.patch.object(vision, "describe_doc_figures", side_effect=fake) as m:
                resp = await main.chat(main.ChatIn(
                    doc_id=1, pages=[1], question="这页的图里画的是什么结构", history=None))
                return await _drain(resp), m

        events, m = asyncio.run(run())
        m.assert_called_once()
        self.assertEqual(m.call_args[0][2], [1])  # 只对未缓存页跑
        self.assertIn("【图中内容（AI 识图）】", captured["user"])
        self.assertIn("binary search flowchart", captured["user"])
        row = db.get_conn().execute(
            "SELECT fig_desc FROM document_pages WHERE doc_id=1 AND page_no=1"
        ).fetchone()
        self.assertIn("二分查找", row["fig_desc"])

    def test_second_chat_uses_cache_no_vision_call(self):
        vision.save_fig_desc(1, 1, "P1 图：已缓存的描述 (cached)。")

        async def fake_stream(messages, usage_out=None):
            yield "第二次回答。"

        async def run():
            with mock.patch.object(main, "stream_chat", fake_stream), \
                 mock.patch.object(vision, "describe_doc_figures",
                                   side_effect=AssertionError("缓存后不应再调 vision")) as m:
                resp = await main.chat(main.ChatIn(
                    doc_id=1, pages=[1], question="这个图讲什么", history=None))
                return await _drain(resp), m

        events, m = asyncio.run(run())
        m.assert_not_called()
        self.assertEqual([t for t, _ in events][-1], "done")

    def test_no_keyword_no_describe(self):
        async def fake_stream(messages, usage_out=None):
            yield "普通回答。"

        # 预判走真实 chat API 是网络依赖（脆弱）：mock chat_complete 固定返回 NO
        async def fake_check(messages, max_tokens=4, temperature=0.0):
            return "NO"

        async def run():
            with mock.patch.object(main, "stream_chat", fake_stream), \
                 mock.patch.object(main, "chat_complete", fake_check), \
                 mock.patch.object(vision, "describe_doc_figures",
                                   side_effect=AssertionError("无图形关键词不应触发")) as m:
                resp = await main.chat(main.ChatIn(
                    doc_id=1, pages=[1], question="这页讲了什么知识点", history=None))
                return await _drain(resp), m

        events, m = asyncio.run(run())
        m.assert_not_called()
        self.assertEqual([t for t, _ in events][-1], "done")

    def test_cached_empty_string_counts_as_done(self):
        # fig_desc=''（已识读过、无图形）→ 不再跑 vision
        vision.save_fig_desc(1, 1, "")

        async def fake_stream(messages, usage_out=None):
            yield "ok"

        async def run():
            with mock.patch.object(main, "stream_chat", fake_stream), \
                 mock.patch.object(vision, "describe_doc_figures",
                                   side_effect=AssertionError("空串缓存后不应再调")) as m:
                resp = await main.chat(main.ChatIn(
                    doc_id=1, pages=[1], question="图里画了什么", history=None))
                return await _drain(resp), m

        events, m = asyncio.run(run())
        m.assert_not_called()
        self.assertEqual([t for t, _ in events][-1], "done")

    def test_pdf_missing_skips_describe(self):
        # PDF 文件不在盘上 → 静默跳过识读，chat 照常
        conn = db.get_conn()
        conn.execute("UPDATE documents SET pdf_path='/nonexistent/x.pdf' WHERE id=1")
        conn.commit()

        async def fake_stream(messages, usage_out=None):
            yield "照常回答。"

        async def run():
            with mock.patch.object(main, "stream_chat", fake_stream), \
                 mock.patch.object(vision, "describe_doc_figures",
                                   side_effect=AssertionError("PDF 缺失不应触发")) as m:
                resp = await main.chat(main.ChatIn(
                    doc_id=1, pages=[1], question="图里画了什么", history=None))
                return await _drain(resp), m

        events, m = asyncio.run(run())
        m.assert_not_called()
        self.assertEqual([t for t, _ in events][-1], "done")

    def test_pages_endpoint_has_fig_field(self):
        resp = main.get_doc_pages(1)
        pages = {p["page_no"]: p for p in resp["pages"]}
        self.assertIs(pages[1]["has_fig"], False)
        vision.save_fig_desc(1, 2, "有图")
        resp2 = main.get_doc_pages(1)
        pages2 = {p["page_no"]: p for p in resp2["pages"]}
        self.assertIs(pages2[2]["has_fig"], True)
        self.assertIs(pages2[1]["has_fig"], False)


class PromptUpgradeTests(unittest.TestCase):
    def test_vision_prompt_mentions_figure_section(self):
        self.assertIn("【图中内容】", vision.VISION_PROMPT)
        self.assertIn("波形", vision.VISION_PROMPT)

    def test_figure_desc_prompt_contract(self):
        self.assertIn("无图形", vision.FIGURE_DESC_PROMPT)
        self.assertIn("circuit diagram", vision.FIGURE_DESC_PROMPT)
        self.assertIn("waveform", vision.FIGURE_DESC_PROMPT)

    def test_build_figure_desc_messages_shape(self):
        msgs = vision.build_figure_desc_messages("QUJD")
        self.assertEqual(len(msgs), 1)
        parts = msgs[0]["content"]
        self.assertEqual(parts[0]["type"], "text")
        self.assertEqual(parts[0]["text"], vision.FIGURE_DESC_PROMPT)
        self.assertEqual(parts[1]["image_url"]["url"], "data:image/png;base64,QUJD")


if __name__ == "__main__":
    unittest.main()
