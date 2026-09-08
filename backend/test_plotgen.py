"""P2.1 精确绘图链路单测（安全求值硬红线 + 各 kind 真渲染冒烟 + 转换 mock）。

覆盖：
- expr 白名单安全求值：合法数学表达式通过；__import__/os/getattr/属性链注入必须被拒
- function spec 校验（缺 xrange / 非法 expr / 坏 points 拒绝）
- resolve_plot_instruction：JSON kind 解析 / 纯文本兜底 / geometry 识别
- 每种 kind 至少一次真渲染冒烟：输出是 PNG、尺寸合理、真值表行列数正确
- chat 转换（mock）：合法 JSON → generate_plot 出 PNG；非法表达式 / 垃圾输出 → PlotSpecError
- geometry/other kind → PlotGeometryFallback（上游降级 image-model）
"""
import asyncio
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import plotgen
from plotgen import (
    PlotGeometryFallback,
    PlotSpecError,
    generate_plot,
    render_boolean,
    render_circuit,
    render_function,
    render_spec,
    render_waveform,
    resolve_plot_instruction,
    safe_eval_expr,
    validate_boolean_spec,
    validate_expr_ast,
)

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def png_size(data: bytes) -> tuple[int, int]:
    """解析 PNG IHDR 宽高。"""
    assert data.startswith(PNG_SIG), "不是 PNG"
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def run(coro):
    return asyncio.run(coro)


class SafeEvalTests(unittest.TestCase):
    """硬红线：注入必须被拒。"""

    def test_accepts_math_expressions(self):
        self.assertAlmostEqual(safe_eval_expr("x**2 + 2*x + 1", 2.0), 9.0)
        self.assertAlmostEqual(safe_eval_expr("2*x", 0.0), 0.0)  # 直线严格过原点
        self.assertAlmostEqual(safe_eval_expr("sqrt(x)", 4.0), 2.0)
        self.assertAlmostEqual(safe_eval_expr("sin(pi*x)", 0.5), 1.0)
        self.assertAlmostEqual(safe_eval_expr("log2(x)", 8.0), 3.0)
        self.assertAlmostEqual(safe_eval_expr("-x**2 + 3", 2.0), -1.0)

    def test_domain_error_returns_nan(self):
        import math

        self.assertTrue(math.isnan(safe_eval_expr("sqrt(x)", -1.0)))
        self.assertTrue(math.isnan(safe_eval_expr("1/x", 0.0)))
        self.assertTrue(math.isnan(safe_eval_expr("log(x)", -2.0)))

    def test_rejects_import_injection(self):
        for payload in (
            "__import__('os')",
            "__import__('os').system('id')",
            "getattr(__builtins__, 'open')",
            "().__class__.__bases__[0].__subclasses__()",
            "os.system('id')",
            "open('/etc/passwd')",
            "[x for x in range(3)]",          # 推导式禁止
            "lambda x: x",                    # lambda 禁止
            "x.__class__",                    # 属性访问禁止
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(PlotSpecError):
                    safe_eval_expr(payload, 1.0)

    def test_validate_expr_ast_rejects_unknown_name(self):
        import ast

        with self.assertRaises(PlotSpecError):
            validate_expr_ast(ast.parse("evil(1)", mode="eval"))


class FunctionSpecTests(unittest.TestCase):
    def test_valid_spec_passes(self):
        spec = {
            "curves": [{"expr": "x**2", "label": "y=x^2"}, {"expr": "2*x", "label": "y=2x"}],
            "xrange": [-3, 3],
            "yrange": None,
            "points": [{"x": 0, "y": 0, "label": "(0,0)"}, {"x": 2, "y": 4, "label": "(2,4)"}],
        }
        plotgen.validate_function_spec(spec)  # 不抛即过

    def test_missing_xrange_rejected(self):
        with self.assertRaises(PlotSpecError):
            plotgen.validate_function_spec({"curves": [{"expr": "x**2"}]})

    def test_invalid_expr_rejected(self):
        with self.assertRaises(PlotSpecError):
            plotgen.validate_function_spec(
                {"curves": [{"expr": "__import__('os')"}], "xrange": [-3, 3]}
            )

    def test_bad_points_rejected(self):
        with self.assertRaises(PlotSpecError):
            plotgen.validate_function_spec(
                {"curves": [{"expr": "x"}], "xrange": [0, 1], "points": [{"x": "a", "y": 0}]}
            )

    def test_inverted_xrange_rejected(self):
        with self.assertRaises(PlotSpecError):
            plotgen.validate_function_spec({"curves": [{"expr": "x"}], "xrange": [3, -3]})


class ResolveInstructionTests(unittest.TestCase):
    def test_json_with_kind(self):
        raw = json.dumps({"kind": "function", "instruction": "plot y=x^2, xrange -3 to 3"})
        kind, text = resolve_plot_instruction(raw)
        self.assertEqual(kind, "function")
        self.assertEqual(text, "plot y=x^2, xrange -3 to 3")

    def test_plain_text_defaults_to_function(self):
        kind, text = resolve_plot_instruction("plot y=2x from -2 to 2")
        self.assertEqual(kind, "function")
        self.assertEqual(text, "plot y=2x from -2 to 2")

    def test_geometry_kind_detected(self):
        kind, _ = resolve_plot_instruction('{"kind": "geometry", "instruction": "a cube"}')
        self.assertEqual(kind, "geometry")

    def test_fallback_prompt_strips_json(self):
        self.assertEqual(
            plotgen.fallback_prompt('{"kind": "function", "instruction": "plot y=x"}'),
            "plot y=x",
        )


class RenderSmokeTests(unittest.TestCase):
    """每种 kind 至少一次真渲染，验证 PNG 字节 + 尺寸。"""

    def test_render_function_png(self):
        spec = {
            "kind": "function",
            "curves": [{"expr": "x**2", "label": "y=x^2"}, {"expr": "2*x", "label": "y=2x"}],
            "xrange": [-3, 3],
            "yrange": None,
            "points": [{"x": 0, "y": 0, "label": "(0,0)"}, {"x": 2, "y": 4, "label": "(2,4)"}],
            "title": "y=x^2 and y=2x",
        }
        data = render_function(spec)
        w, h = png_size(data)
        self.assertGreater(w, 300)
        self.assertGreater(h, 200)
        # 存临时文件验证可写 PNG
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "fn.png"
            p.write_bytes(data)
            self.assertEqual(p.read_bytes()[:8], PNG_SIG)

    def test_render_waveform_png(self):
        spec = {
            "kind": "waveform",
            "signals": [
                {"name": "CLK", "wave": "01010101"},
                {"name": "Q0", "wave": "00110011"},
                {"name": "Q1", "wave": "00001111"},
            ],
            "title": "2-bit counter",
        }
        data = render_waveform(spec)
        w, h = png_size(data)
        self.assertGreater(w, 300)
        self.assertGreater(h, 150)

    def test_render_boolean_png_and_table_shape(self):
        spec = {
            "kind": "boolean",
            "expr": "Y=(A·B)+C̄",
            "inputs": ["A", "B", "C"],
            "output": "Y",
            "truth_table": [
                [0, 0, 0, 1], [0, 0, 1, 0], [0, 1, 0, 1], [0, 1, 1, 0],
                [1, 0, 0, 1], [1, 0, 1, 0], [1, 1, 0, 1], [1, 1, 1, 1],
            ],
            "gates": [
                {"type": "AND", "inputs": ["A", "B"], "out": "n1"},
                {"type": "NOT", "inputs": ["C"], "out": "n0"},
                {"type": "OR", "inputs": ["n1", "n0"], "out": "Y"},
            ],
        }
        nrows, ncols = validate_boolean_spec(spec)
        self.assertEqual(nrows, 8)   # 2^3 行
        self.assertEqual(ncols, 4)   # A B C Y
        data = render_boolean(spec)
        w, h = png_size(data)
        self.assertGreater(w, 400)
        self.assertGreater(h, 200)

    def test_render_circuit_png(self):
        spec = {
            "kind": "circuit",
            "elements": [
                {"type": "V", "at": [0, 0], "to": [0, 3], "label": "5V"},
                {"type": "R", "at": [0, 3], "to": [4, 3], "label": "1kΩ"},
                {"type": "LED", "at": [4, 3], "to": [4, 0], "label": "LED"},
                {"type": "W", "at": [4, 0], "to": [0, 0]},
            ],
            "title": "Series circuit",
        }
        data = render_circuit(spec)
        w, h = png_size(data)
        self.assertGreater(w, 200)
        self.assertGreater(h, 100)

    def test_render_circuit_unknown_element_rejected(self):
        spec = {"kind": "circuit", "elements": [{"type": "NuclearReactor", "at": [0, 0]}]}
        with self.assertRaises(PlotSpecError):
            render_circuit(spec)

    def test_render_spec_dispatch_unknown_kind(self):
        with self.assertRaises(plotgen.PlotError):
            render_spec({"kind": "nope"})


class ConverterTests(unittest.TestCase):
    """chat 转换（mock chat_complete）+ 降级路径。"""

    def test_generate_plot_function_success(self):
        spec_json = json.dumps({
            "kind": "function",
            "curves": [{"expr": "2*x", "label": "y=2x"}, {"expr": "x**2", "label": "y=x^2"}],
            "xrange": [-3, 3],
            "yrange": None,
            "points": [{"x": 0, "y": 0, "label": "(0,0)"}, {"x": 2, "y": 4, "label": "(2,4)"}],
        })
        with mock.patch.object(plotgen, "chat_complete", return_value=spec_json) as fake:
            data = run(generate_plot('{"kind": "function", "instruction": "plot y=2x and y=x^2"}'))
        fake.assert_called_once()
        self.assertTrue(data.startswith(PNG_SIG))

    def test_generate_plot_rejects_injected_expr_from_chat(self):
        spec_json = json.dumps({
            "curves": [{"expr": "__import__('os')", "label": "evil"}],
            "xrange": [-3, 3],
        })
        with mock.patch.object(plotgen, "chat_complete", return_value=spec_json):
            with self.assertRaises(PlotSpecError):
                run(generate_plot("plot something"))

    def test_generate_plot_rejects_garbage_chat_output(self):
        with mock.patch.object(plotgen, "chat_complete", return_value="抱歉，我不太明白"):
            with self.assertRaises(PlotSpecError):
                run(generate_plot("plot y=x"))

    def test_generate_plot_geometry_raises_fallback(self):
        with self.assertRaises(PlotGeometryFallback):
            run(generate_plot('{"kind": "geometry", "instruction": "draw a cube"}'))

    def test_generate_plot_other_raises_fallback(self):
        with self.assertRaises(PlotGeometryFallback):
            run(generate_plot('{"kind": "other", "instruction": "random sketch"}'))

    def test_chat_json_with_fences_parsed(self):
        spec_json = "```json\n" + json.dumps({
            "curves": [{"expr": "x", "label": "y=x"}], "xrange": [0, 1],
        }) + "\n```"
        with mock.patch.object(plotgen, "chat_complete", return_value=spec_json):
            data = run(generate_plot("plot y=x"))
        self.assertTrue(data.startswith(PNG_SIG))


class MathtextSanitizerTests(unittest.TestCase):
    """兜底 sanitizer：裸写数学量 → $...$；expr 绝不被改。"""

    def test_common_bare_tokens_wrapped(self):
        cases = {
            "r_pi = 2 kOhm": "$r_{\\pi}$ = 2 kΩ",
            "g_m v_pi": "$g_m$ $v_{\\pi}$",
            "y=x^2": "y=$x^{2}$",
            "R_C = 4.7 kOhm": "$R_{C}$ = 4.7 kΩ",
            "V_cc and V_be": "$V_{cc}$ and $V_{be}$",
            "output v_o": "output $v_o$",
        }
        for src, want in cases.items():
            with self.subTest(src=src):
                self.assertEqual(plotgen.sanitize_mathtext(src), want)

    def test_already_mathtext_untouched(self):
        # 已含 $ 的文本整体跳过（模型已按 mathtext 写）
        s = "$y=x^2$ and $g_m v_{\\pi}$"
        self.assertEqual(plotgen.sanitize_mathtext(s), s)

    def test_no_false_positive_on_words(self):
        for s in ("half_pi filter", "lambda_o sets", "plain text no math"):
            self.assertEqual(plotgen.sanitize_mathtext(s), s)

    def test_sanitize_spec_labels_only_not_expr(self):
        spec = {
            "kind": "function",
            "curves": [{"expr": "x**2", "label": "y=x^2"}, {"expr": "exp(-x**2)", "label": "y=e^-x^2"}],
            "xrange": [-3, 3],
            "points": [{"x": 0, "y": 1, "label": "peak at x^2=0"}],
            "title": "g_m vs r_pi curve",
        }
        out = plotgen.sanitize_spec_labels(spec)
        # expr 一字不动（求值红线）
        self.assertEqual(out["curves"][0]["expr"], "x**2")
        self.assertEqual(out["curves"][1]["expr"], "exp(-x**2)")
        # label/title 被 mathtext 化
        self.assertEqual(out["curves"][0]["label"], "y=$x^{2}$")
        self.assertIn("$g_m$", out["title"])
        self.assertIn("$r_{\\pi}$", out["title"])
        # points label 也处理
        self.assertIn("$x^{2}$", out["points"][0]["label"])

    def test_circuit_labels_sanitized(self):
        spec = {
            "kind": "circuit",
            "elements": [
                {"type": "R", "at": [0, 0], "to": [2, 0], "label": "r_pi = 2 kOhm"},
                {"type": "V", "at": [0, 0], "to": [0, 2], "label": "V_cc"},
            ],
            "title": "hybrid-pi with g_m",
        }
        out = plotgen.sanitize_spec_labels(spec)
        self.assertEqual(out["elements"][0]["label"], "$r_{\\pi}$ = 2 kΩ")
        self.assertEqual(out["elements"][1]["label"], "$V_{cc}$")
        self.assertIn("$g_m$", out["title"])

    def test_convert_prompt_requires_mathtext(self):
        """转换 system prompt 必须含 mathtext 指令（$...$ 写法）。"""
        for kind in ("function", "waveform", "boolean", "circuit"):
            msgs = plotgen._convert_messages(kind, "plot something")
            sys_text = msgs[0]["content"]
            with self.subTest(kind=kind):
                self.assertIn("$", sys_text)
                self.assertIn("mathtext", sys_text)
        # circuit/function 明确禁止裸 r_pi/g_m
        self.assertIn("r_pi", plotgen._convert_messages("circuit", "x")[0]["content"])
        self.assertIn("$y=x^2$", plotgen._convert_messages("function", "x")[0]["content"])

    def test_convert_to_spec_applies_sanitizer(self):
        """chat 输出裸 label → _convert_to_spec 返回的 spec 已被兜底 mathtext 化。"""
        spec_json = json.dumps({
            "curves": [{"expr": "x**2", "label": "y=x^2"}],
            "xrange": [-3, 3],
            "title": "y=x^2 vs g_m",
        })
        with mock.patch.object(plotgen, "chat_complete", return_value=spec_json):
            spec = run(plotgen._convert_to_spec("function", "plot y=x^2"))
        self.assertEqual(spec["curves"][0]["label"], "y=$x^{2}$")
        self.assertIn("$g_m$", spec["title"])
        self.assertEqual(spec["curves"][0]["expr"], "x**2")  # expr 不动


class CaptionResolveTests(unittest.TestCase):
    """marker JSON caption 读取。"""

    def test_caption_read_from_marker_json(self):
        raw = json.dumps({
            "kind": "circuit",
            "instruction": "PNP BJT hybrid-pi small-signal model",
            "caption": "PNP BJT hybrid-pi small-signal model. PNP 三极管 hybrid-π 小信号模型。",
        })
        self.assertEqual(
            plotgen.resolve_plot_caption(raw),
            "PNP BJT hybrid-pi small-signal model. PNP 三极管 hybrid-π 小信号模型。",
        )

    def test_no_caption_returns_empty(self):
        self.assertEqual(plotgen.resolve_plot_caption('{"kind": "function", "instruction": "plot y=x"}'), "")
        self.assertEqual(plotgen.resolve_plot_caption("plot y=x"), "")

    def test_instruction_still_resolves_with_caption_present(self):
        raw = json.dumps({
            "kind": "function",
            "instruction": "plot y=x^2",
            "caption": "Parabola. 抛物线。",
        })
        kind, text = resolve_plot_instruction(raw)
        self.assertEqual(kind, "function")
        self.assertEqual(text, "plot y=x^2")


if __name__ == "__main__":
    unittest.main()
