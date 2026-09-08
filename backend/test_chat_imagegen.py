"""P2 画图题出图链路单测（mock imagegen，不触网）。

覆盖：
- extract_draw_prompts：标记解析 + 剥离（中英文冒号、独立行、多个标记、无标记）
- DrawFilterBuffer：跨 delta 切片的标记不外漏（打字机分片边界任意切）
- /api/chat SSE：delta 正常下发、image 事件推送、标记原文不出现在正文
- 出图失败：image 事件带 error 字段，正文不受影响
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import main
import providers


PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class ChatImageTests(unittest.TestCase):
    def setUp(self):
        old = getattr(db._local, "conn", None)
        if old is not None:
            old.close()
            del db._local.conn
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / "test.db"
        db.DATA_DIR = Path(self.tmp.name)
        db.init_db()
        self._old_images_dir = main.IMAGES_DIR
        main.IMAGES_DIR = Path(self.tmp.name) / "images"
        main.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        conn = db.get_conn()
        conn.execute("INSERT INTO subjects(id,name,sort_order) VALUES(1,'数学',0)")
        conn.execute(
            "INSERT INTO documents(id,subject_id,folder_id,filename,orig_path,pdf_path,page_count,sort_order)"
            " VALUES(1,1,NULL,'讲义.pdf','','',1,0)"
        )
        conn.execute(
            "INSERT INTO document_pages(doc_id,page_no,text,lang,source,width,height)"
            " VALUES(1,1,'极限与连续 内容','zh','text',595,842)"
        )
        conn.commit()

    def tearDown(self):
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
            del db._local.conn
        main.IMAGES_DIR = self._old_images_dir
        self.tmp.cleanup()

    # ---------- extract_draw_prompts：完整文本一次性解析 ----------

    def test_extract_and_strip_marker(self):
        text = "先解方程。\n\n[DRAW: parabola y=x^2 and line y=2x on axes]\n\n结论如上。"
        cleaned, prompts = main.extract_draw_prompts(text)
        self.assertEqual(prompts, ["parabola y=x^2 and line y=2x on axes"])
        self.assertNotIn("[DRAW", cleaned)
        self.assertIn("先解方程。", cleaned)
        self.assertIn("结论如上。", cleaned)

    def test_extract_chinese_colon_and_multiple(self):
        text = "a\n[DRAW：unit circle]b\n[DRAW: tangent line](end)"
        cleaned, prompts = main.extract_draw_prompts(text)
        self.assertEqual(prompts, ["unit circle", "tangent line"])
        self.assertNotIn("DRAW", cleaned)

    def test_extract_none(self):
        cleaned, prompts = main.extract_draw_prompts("普通回答，无标记。[BRACKET] 不是标记")
        self.assertEqual(prompts, [])
        self.assertIn("[BRACKET]", cleaned)

    # ---------- DrawFilterBuffer：任意切片不外漏 ----------

    def test_filter_buffer_arbitrary_splits(self):
        full = (
            "## 思路\n先求交点。\n\n[DRAW: axes with parabola and line, intersections marked]\n\n"
            "第二步...\n中间有个数组 arr[i] 不受影响。\n[DRAW: zoomed view at origin]\n完毕。"
        )
        for size in (1, 3, 7, 13, 40, len(full)):
            with self.subTest(chunk=size):
                filt = main.DrawFilterBuffer()
                out = []
                for i in range(0, len(full), size):
                    out.append(filt.feed(full[i : i + size]))
                out.append(filt.flush())
                joined = "".join(out)
                self.assertNotIn("[DRAW", joined)
                self.assertNotIn("DRAW:", joined)
                self.assertIn("先求交点。", joined)
                self.assertIn("arr[i]", joined)
                self.assertIn("完毕。", joined)
                self.assertEqual(
                    filt.prompts,
                    ["axes with parabola and line, intersections marked", "zoomed view at origin"],
                )

    def test_filter_buffer_plain_text_passthrough(self):
        filt = main.DrawFilterBuffer()
        out = filt.feed("普通文本 [B] 与 [DR 元素都放行")
        self.assertEqual(out, "普通文本 [B] 与 [DR 元素都放行")

    # ---------- SSE 端到端（mock stream_chat + generate_image） ----------

    def _run_chat(self, answer: str):
        async def fake_stream(messages, usage_out=None):
            for i in range(0, len(answer), 5):
                yield answer[i : i + 5]

        async def collect():
            events = []
            with mock.patch.object(main, "stream_chat", fake_stream):
                body = main.ChatIn(doc_id=1, pages=[1], question="画出 y=x^2", history=None)
                resp = await main.chat(body)
                agen = resp.body_iterator
                async for chunk in agen:
                    for block in chunk.split("\n\n"):
                        if not block.strip():
                            continue
                        lines = block.split("\n")
                        event = lines[0][len("event: "):]
                        data = json.loads(lines[1][len("data: "):])
                        events.append((event, data))
            return events

        return asyncio.run(collect())

    def test_chat_sse_emits_image_event_and_strips_marker(self):
        answer = "解：先联立方程。\n\n[DRAW: coordinate plane, parabola y=x^2 and line y=2x, intersections at 0 and 2]\n\n如图为交点。"
        with mock.patch.object(main, "generate_image", return_value=PNG_1PX) as fake_gen:
            events = self._run_chat(answer)
        # 出图被调用 1 次，参数是标记里的英文描述
        fake_gen.assert_called_once_with("coordinate plane, parabola y=x^2 and line y=2x, intersections at 0 and 2")
        types = [t for t, _ in events]
        self.assertIn("image", types)
        self.assertEqual(types[-1], "done")
        delta_text = "".join(d["content"] for t, d in events if t == "delta")
        self.assertNotIn("[DRAW", delta_text)
        self.assertIn("解：先联立方程。", delta_text)
        self.assertIn("如图为交点。", delta_text)
        img_events = [d for t, d in events if t == "image"]
        self.assertEqual(len(img_events), 1)
        self.assertIn("/api/images/", img_events[0]["url"])
        # 文件真的落盘 & 可通过静态路由读到
        name = img_events[0]["url"].rsplit("/", 1)[1]
        saved = main.IMAGES_DIR / name
        self.assertTrue(saved.is_file())
        self.assertEqual(saved.read_bytes(), PNG_1PX)
        resp = main.get_image(name)
        self.assertEqual(resp.media_type, "image/png")
        # done 在 image 之后
        self.assertLess(types.index("image"), len(types) - 1)

    def test_chat_sse_image_failure_sends_error_field(self):
        answer = "看图。\n[DRAW: circle]"
        with mock.patch.object(main, "generate_image", side_effect=RuntimeError("gateway 503")):
            events = self._run_chat(answer)
        types = [t for t, _ in events]
        self.assertIn("image", types)
        img = [d for t, d in events if t == "image"][0]
        self.assertIn("gateway 503", img["error"])
        self.assertEqual(img["prompt"], "circle")
        # 正文不受影响，done 正常收尾
        delta_text = "".join(d["content"] for t, d in events if t == "delta")
        self.assertIn("看图。", delta_text)
        self.assertNotIn("[DRAW", delta_text)
        self.assertEqual(types[-1], "done")

    def test_chat_sse_no_marker_no_image_event(self):
        events = self._run_chat("普通回答。")
        types = [t for t, _ in events]
        self.assertNotIn("image", types)
        delta_text = "".join(d["content"] for t, d in events if t == "delta")
        self.assertEqual(delta_text, "普通回答。")

    def test_imagegen_payload_and_extract(self):
        import base64

        import imagegen

        with mock.patch.object(
            imagegen,
            "get_provider",
            return_value=providers.ProviderConfig(
                role="image_gen", base_url="https://x.example/v1", api_key="k", model="test-img-model"
            ),
        ):
            payload = imagegen.build_request_payload("a cat", "1024x1024")
        self.assertEqual(payload["model"], "test-img-model")
        self.assertEqual(payload["prompt"], "a cat")

        img = imagegen.extract_image_bytes({"data": [{"b64_json": base64.b64encode(PNG_1PX).decode()}]})
        self.assertEqual(img, PNG_1PX)
        self.assertIsNone(imagegen.extract_image_bytes({"data": []}))
        self.assertIsNone(imagegen.extract_image_bytes({"data": [{"b64_json": "!!!not b64!!!"}]}))

    # ---------- P2.1：[PLOT] 标记（精确绘制）+ 降级路径 ----------

    PLOT_MARK = (
        '[PLOT: {"kind": "function", "instruction": "plot y=2x and y=x^2, '
        'xrange -3 to 3, mark intersections (0,0) and (2,4)"}]'
    )

    def test_plot_marker_stripped_and_plot_event(self):
        from plotgen import PlotError  # noqa: F401 —— 模块可导入（依赖就绪）

        answer = f"先联立方程。\n\n{self.PLOT_MARK}\n\n如图为交点。"
        with mock.patch.object(main, "generate_plot", return_value=PNG_1PX) as fake_plot:
            events = self._run_chat(answer)
        fake_plot.assert_called_once()
        self.assertEqual(
            fake_plot.call_args[0][0],
            '{"kind": "function", "instruction": "plot y=2x and y=x^2, xrange -3 to 3, mark intersections (0,0) and (2,4)"}',
        )
        delta_text = "".join(d["content"] for t, d in events if t == "delta")
        self.assertNotIn("[PLOT", delta_text)
        self.assertIn("先联立方程。", delta_text)
        img = [d for t, d in events if t == "image"]
        self.assertEqual(len(img), 1)
        self.assertEqual(img[0]["source"], "plot")  # function → plot
        self.assertEqual(img[0]["prompt"], "plot y=2x and y=x^2, xrange -3 to 3, mark intersections (0,0) and (2,4)")
        self.assertNotIn("degraded", img[0])
        self.assertIn("/api/images/", img[0]["url"])
        name = img[0]["url"].rsplit("/", 1)[1]
        self.assertEqual((main.IMAGES_DIR / name).read_bytes(), PNG_1PX)

    def test_plot_failure_falls_back_to_draw_with_degraded(self):
        from plotgen import PlotError

        answer = f"看图。\n{self.PLOT_MARK}"
        with mock.patch.object(
            main, "generate_plot", side_effect=PlotError("表达式非法")
        ), mock.patch.object(main, "generate_image", return_value=PNG_1PX) as fake_img:
            events = self._run_chat(answer)
        fake_img.assert_called_once_with(
            "plot y=2x and y=x^2, xrange -3 to 3, mark intersections (0,0) and (2,4)"
        )
        img = [d for t, d in events if t == "image"][0]
        self.assertEqual(img["source"], "draw")
        self.assertTrue(img.get("degraded"))  # caption 标注「降级生成」
        self.assertIn("/api/images/", img["url"])
        delta_text = "".join(d["content"] for t, d in events if t == "delta")
        self.assertNotIn("[PLOT", delta_text)

    def test_plot_geometry_kind_falls_back_to_draw(self):
        from plotgen import PlotGeometryFallback

        answer = '[PLOT: {"kind": "geometry", "instruction": "a 3D cube sketch"}]'
        with mock.patch.object(
            main, "generate_plot", side_effect=PlotGeometryFallback("geometry 走生图")
        ), mock.patch.object(main, "generate_image", return_value=PNG_1PX) as fake_img:
            events = self._run_chat(answer)
        fake_img.assert_called_once_with("a 3D cube sketch")
        img = [d for t, d in events if t == "image"][0]
        self.assertEqual(img["source"], "draw")
        self.assertTrue(img.get("degraded"))

    def test_dual_markers_emitted_in_order(self):
        answer = (
            f"函数图如下。\n{self.PLOT_MARK}\n"
            "几何示意如下。\n[DRAW: 3D cube with shading]\n完毕。"
        )
        with mock.patch.object(main, "generate_plot", return_value=PNG_1PX), mock.patch.object(
            main, "generate_image", return_value=PNG_1PX
        ):
            events = self._run_chat(answer)
        imgs = [d for t, d in events if t == "image"]
        self.assertEqual([i["source"] for i in imgs], ["plot", "draw"])  # 保持标记出现顺序
        delta_text = "".join(d["content"] for t, d in events if t == "delta")
        self.assertNotIn("[PLOT", delta_text)
        self.assertNotIn("[DRAW", delta_text)
        self.assertIn("函数图如下。", delta_text)
        self.assertIn("完毕。", delta_text)

    def test_plot_marker_caption_used_as_prompt(self):
        """marker JSON 带 caption（中英双语）→ image 事件 prompt 用 caption。"""
        answer = (
            '[PLOT: {"kind": "circuit", "instruction": "PNP BJT hybrid-pi model with r_pi and g_m", '
            '"caption": "PNP BJT small-signal hybrid-pi model. PNP 三极管 hybrid-π 小信号模型。"}]'
        )
        with mock.patch.object(main, "generate_plot", return_value=PNG_1PX) as fake_plot:
            events = self._run_chat(answer)
        fake_plot.assert_called_once()
        img = [d for t, d in events if t == "image"][0]
        self.assertEqual(img["source"], "circuit")
        self.assertEqual(img["prompt"], "PNP BJT small-signal hybrid-pi model. PNP 三极管 hybrid-π 小信号模型。")
        self.assertNotIn("degraded", img)

    def test_plot_marker_without_caption_falls_back_to_instruction(self):
        """无 caption → prompt 退回 instruction（兼容旧格式 marker）。"""
        with mock.patch.object(main, "generate_plot", return_value=PNG_1PX):
            events = self._run_chat(self.PLOT_MARK)
        img = [d for t, d in events if t == "image"][0]
        self.assertEqual(img["prompt"], "plot y=2x and y=x^2, xrange -3 to 3, mark intersections (0,0) and (2,4)")

    def test_plot_fallback_keeps_caption_as_prompt(self):
        """PLOT 失败降级生图：prompt 仍是 marker caption（双语图注不丢）。"""
        from plotgen import PlotError

        answer = (
            '[PLOT: {"kind": "circuit", "instruction": "PNP hybrid-pi", '
            '"caption": "PNP hybrid-pi model. PNP hybrid-π 模型。"}]'
        )
        with mock.patch.object(
            main, "generate_plot", side_effect=PlotError("渲染失败")
        ), mock.patch.object(main, "generate_image", return_value=PNG_1PX) as fake_img:
            events = self._run_chat(answer)
        fake_img.assert_called_once_with("PNP hybrid-pi")  # 生图读英文 instruction
        img = [d for t, d in events if t == "image"][0]
        self.assertEqual(img["source"], "draw")
        self.assertTrue(img.get("degraded"))
        self.assertEqual(img["prompt"], "PNP hybrid-pi model. PNP hybrid-π 模型。")

    def test_system_prompt_has_bilingual_draw_and_plot_caption(self):
        """系统提示词：[DRAW] 双语格式 + [PLOT] caption 字段要求已就位。"""
        import prompts

        self.assertIn("caption", prompts.SYSTEM_PROMPT)
        self.assertIn("[DRAW: 英文一句描述。中文对照一句。]", prompts.SYSTEM_PROMPT)

    def test_filter_buffer_plot_arbitrary_splits(self):
        full = (
            "## 思路\n先求交点。\n\n"
            + self.PLOT_MARK
            + "\n\n第二步...\n"
            + '[PLOT: {"kind": "waveform", "instruction": "CLK and Q0 of a counter"}]\n'
            "完毕。"
        )
        for size in (1, 3, 7, 13, 40, len(full)):
            with self.subTest(chunk=size):
                filt = main.DrawFilterBuffer()
                out = []
                for i in range(0, len(full), size):
                    out.append(filt.feed(full[i : i + size]))
                out.append(filt.flush())
                joined = "".join(out)
                self.assertNotIn("[PLOT", joined)
                self.assertNotIn("PLOT:", joined)
                self.assertIn("先求交点。", joined)
                self.assertIn("完毕。", joined)
                self.assertEqual(len(filt.plots), 2)
                self.assertEqual(len(filt.markers), 2)
                self.assertEqual([t for t, _ in filt.markers], ["plot", "plot"])
                self.assertEqual(filt.markers[0][0], "plot")
                self.assertIn('"kind": "function"', filt.plots[0])
                self.assertIn('"kind": "waveform"', filt.plots[1])


if __name__ == "__main__":
    unittest.main()
