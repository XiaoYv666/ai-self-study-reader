"""P2.1：类型化绘图渲染 —— 精确函数图 / 波形图 / 布尔真值表与逻辑门 / 电路图。

标记协议：主 chat 链路剥离 `[PLOT: {...}]` 后把内容交给 generate_plot()：

- 指令 JSON 带 kind（function/waveform/boolean/circuit/geometry/other）
- function / waveform / boolean / circuit：
    1. 由 chat 模型（非流式，复用 llm.py 客户端）把指令文本转成受控的
       kind 专属 JSON（schema 见 _SCHEMA_EXAMPLES，模型只填）
    2. 校验（expr 必须过 AST 白名单求值 —— 硬红线，`__import__`/`os`/`getattr`
       一类注入一律拒绝）→ matplotlib / schemdraw 渲染 PNG bytes
- geometry / other：抛 PlotGeometryFallback，上游降级走 image-model
- 任何转换/校验/渲染失败：抛 PlotError，上游同样降级 image-model（degraded=true）

安全求值设计：
- AST 静态校验：只允许 x 变量、白名单 math 函数、数值常量、四则/幂/一元运算
- eval 命名空间：{"__builtins__": {}} + 白名单函数，杜绝任何导入/属性访问
"""
import ast
import io
import json
import logging
import math
import re
from typing import Any

import matplotlib

matplotlib.use("Agg")  # 无头渲染，必须先于 pyplot
import matplotlib.pyplot as plt  # noqa: E402

from llm import chat_complete  # noqa: E402 —— 绘图指令 → JSON 转换（非流式）

logger = logging.getLogger(__name__)

PLOT_KINDS = {"function", "waveform", "boolean", "circuit", "geometry", "other"}
DPI = 150


class PlotError(Exception):
    """绘图链路失败，上游降级走 image-model。"""


class PlotSpecError(PlotError):
    """指令 JSON / 表达式非法（含注入攻击）。"""


class PlotGeometryFallback(PlotError):
    """geometry/other kind：本模块不渲染，直接降级生图。"""


# ---------------------------------------------------------------------------
# 安全求值（硬红线）
# ---------------------------------------------------------------------------

_ALLOWED_MATH: dict[str, Any] = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "atan2": math.atan2, "sqrt": math.sqrt, "log": math.log,
    "log10": math.log10, "log2": math.log2, "exp": math.exp,
    "abs": abs, "fabs": math.fabs, "floor": math.floor, "ceil": math.ceil,
    "degrees": math.degrees, "radians": math.radians,
    "pi": math.pi, "e": math.e, "tau": math.tau,
}
_EXPR_MAX_LEN = 200


def validate_expr_ast(node: ast.AST) -> None:
    """递归校验表达式 AST，只放行白名单语法。任何其他节点抛 PlotSpecError。"""
    if isinstance(node, ast.Expression):
        validate_expr_ast(node.body)
        return
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return
        raise PlotSpecError(f"expr 含非法常量 {node.value!r}")
    if isinstance(node, ast.Name):
        if node.id == "x" or node.id in _ALLOWED_MATH:
            return
        raise PlotSpecError(f"expr 含未授权名称 {node.id!r}")
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod, ast.FloorDiv)
    ):
        validate_expr_ast(node.left)
        validate_expr_ast(node.right)
        return
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        validate_expr_ast(node.operand)
        return
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_MATH:
            raise PlotSpecError("expr 只允许白名单 math 函数调用")
        if node.keywords:
            raise PlotSpecError("expr 不允许关键字参数")
        for a in node.args:
            validate_expr_ast(a)
        return
    raise PlotSpecError(f"expr 含不允许的语法节点：{type(node).__name__}")


def _safe_globals() -> dict[str, Any]:
    ns: dict[str, Any] = {"__builtins__": {}}
    ns.update(_ALLOWED_MATH)
    return ns


def safe_eval_expr(expr: str, x: float) -> float:
    """白名单安全求值：非法表达式抛 PlotSpecError；合法但定义域外返回 nan。"""
    if not isinstance(expr, str) or not expr or len(expr) > _EXPR_MAX_LEN:
        raise PlotSpecError("expr 必须是非空字符串（≤200 字符）")
    tree = ast.parse(expr, mode="eval")
    validate_expr_ast(tree)
    ns = _safe_globals()
    ns["x"] = float(x)
    try:
        return float(eval(compile(tree, "<plot>", "eval"), ns, {}))  # noqa: S307 —— AST 已白名单校验
    except (ZeroDivisionError, ValueError, OverflowError, TypeError):
        return float("nan")


# ---------------------------------------------------------------------------
# 中文字体（macOS 优先 PingFang SC，图表标签尽量英文以防缺字）
# ---------------------------------------------------------------------------

_CJK_FONT: str | None = None
_CJK_CANDIDATES = (
    "PingFang SC", "Hiragino Sans GB", "Heiti SC", "Arial Unicode MS",
    "STHeiti", "Noto Sans CJK SC", "WenQuanYi Micro Hei",
)


def _setup_cjk() -> str | None:
    global _CJK_FONT
    if _CJK_FONT:
        return _CJK_FONT
    try:
        from matplotlib import font_manager

        available = {f.name for f in font_manager.fontManager.ttflist}
    except Exception:  # noqa: BLE001 —— 字体探测失败不致命
        return None
    for name in _CJK_CANDIDATES:
        if name in available:
            plt.rcParams["font.sans-serif"] = [name] + [
                f for f in plt.rcParams.get("font.sans-serif", []) if f != name
            ]
            plt.rcParams["axes.unicode_minus"] = False
            _CJK_FONT = name
            logger.info("plot_cjk_font=%s", name)
            return name
    return None


def _fig_bytes(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI)
    plt.close(fig)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 指令解析
# ---------------------------------------------------------------------------

def resolve_plot_instruction(instruction: str) -> tuple[str, str]:
    """解析 [PLOT: ...] 内容 → (kind, 指令文本)。

    - JSON 对象且带合法 kind：取 kind + instruction/text 字段
    - 纯文本/JSON 解析失败：默认 function，原文即指令
    """
    raw = (instruction or "").strip()
    try:
        obj = json.loads(raw)
    except Exception:  # noqa: BLE001 —— 非 JSON 一律按纯文本指令处理
        obj = None
    if isinstance(obj, dict) and isinstance(obj.get("kind"), str) and obj["kind"] in PLOT_KINDS:
        kind = obj["kind"]
        text = obj.get("instruction") or obj.get("text") or ""
        return kind, text.strip() or raw
    return "function", raw


def resolve_plot_caption(instruction: str) -> str:
    """读取 [PLOT: ...] marker JSON 自带的 caption 字段（中英双语图注）。

    无 caption / 非 JSON → 空串，上游回退用 instruction。
    """
    raw = (instruction or "").strip()
    try:
        obj = json.loads(raw)
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(obj, dict) and isinstance(obj.get("caption"), str):
        return obj["caption"].strip()
    return ""


def fallback_prompt(instruction: str) -> str:
    """降级走 image-model 时用的英文图片描述（剥掉 JSON 壳）。"""
    _, text = resolve_plot_instruction(instruction)
    return text or instruction


def _parse_json(raw: str) -> Any:
    """容错 JSON 解析：剥 ```json 围栏；失败时取第一个 {...} 块。"""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


# ---------------------------------------------------------------------------
# kind 专属 spec 校验
# ---------------------------------------------------------------------------

def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_function_spec(spec: dict) -> None:
    curves = spec.get("curves")
    if not isinstance(curves, list) or not curves:
        raise PlotSpecError("function spec 缺 curves（非空列表）")
    for c in curves:
        if not isinstance(c, dict):
            raise PlotSpecError("curves 项必须是对象 {expr, label?}")
        expr = c.get("expr")
        if not isinstance(expr, str) or not expr or len(expr) > _EXPR_MAX_LEN:
            raise PlotSpecError("curve.expr 必须是非空字符串（≤200 字符）")
        validate_expr_ast(ast.parse(expr, mode="eval"))  # 注入拒绝点
    xr = spec.get("xrange")
    if (
        not isinstance(xr, list) or len(xr) != 2
        or not all(_is_num(v) for v in xr) or not (xr[0] < xr[1])
    ):
        raise PlotSpecError("xrange 必须是 [min, max] 且 min<max")
    yr = spec.get("yrange")
    if yr is not None and (
        not isinstance(yr, list) or len(yr) != 2
        or not all(_is_num(v) for v in yr) or not (yr[0] < yr[1])
    ):
        raise PlotSpecError("yrange 必须是 [min, max] 或 null")
    pts = spec.get("points") or []
    if not isinstance(pts, list):
        raise PlotSpecError("points 必须是列表")
    for p in pts:
        if not isinstance(p, dict) or not all(_is_num(p.get(k)) for k in ("x", "y")):
            raise PlotSpecError("points 项必须是 {x, y, label?}")


_WAVE_CHARS = {"0", "1", "p", "n", "x", "h", "l"}
_WAVE_LEVEL = {"0": 0.0, "l": 0.0, "n": 0.0, "1": 1.0, "h": 1.0, "p": 1.0, "x": 0.5}


def validate_waveform_spec(spec: dict) -> int:
    """校验波形 spec，返回信号最长槽位数（各信号等长校验）。"""
    signals = spec.get("signals")
    if not isinstance(signals, list) or not signals:
        raise PlotSpecError("waveform spec 缺 signals（非空列表）")
    lengths = set()
    for s in signals:
        if not isinstance(s, dict):
            raise PlotSpecError("signals 项必须是对象 {name, wave}")
        wave = s.get("wave")
        if not isinstance(wave, str) or not wave:
            raise PlotSpecError("signal.wave 必须是非空字符串")
        for ch in wave:
            if ch not in _WAVE_CHARS:
                raise PlotSpecError(f"wave 含非法字符 {ch!r}（只允许 0/1/p/n/x/h/l）")
        lengths.add(len(wave))
    if len(lengths) > 1:
        raise PlotSpecError(f"各信号 wave 长度必须一致：{sorted(lengths)}")
    return lengths.pop()


def validate_boolean_spec(spec: dict) -> tuple[int, int]:
    """校验布尔 spec，返回 (行数, 列数)（真值表行列数，测试用）。"""
    inputs = spec.get("inputs") or []
    if not isinstance(inputs, list) or not inputs:
        raise PlotSpecError("boolean spec 缺 inputs（非空列表）")
    output = spec.get("output") or "Y"
    if not isinstance(output, str):
        raise PlotSpecError("output 必须是字符串")
    tt = spec.get("truth_table")
    if not isinstance(tt, list) or not tt:
        raise PlotSpecError("boolean spec 缺 truth_table")
    ncols = len(inputs) + 1
    for row in tt:
        if not isinstance(row, list) or len(row) != ncols or not all(
            v in (0, 1) for v in row
        ):
            raise PlotSpecError("truth_table 每行必须是 0/1 列表，长度=输入数+1")
    gates = spec.get("gates")
    if gates is not None:
        if not isinstance(gates, list) or not gates:
            raise PlotSpecError("gates 必须是非空列表或省略")
        for g in gates:
            if not isinstance(g, dict) or not isinstance(g.get("type"), str):
                raise PlotSpecError("gates 项必须是 {type, inputs, out}")
            if g["type"] not in _GATE_TYPES:
                raise PlotSpecError(f"门类型不支持：{g['type']!r}（白名单 {sorted(_GATE_TYPES)}）")
            if not isinstance(g.get("inputs"), list):
                raise PlotSpecError("gate.inputs 必须是列表")
            if not isinstance(g.get("out"), str) or not g["out"]:
                raise PlotSpecError("gate.out 必须是非空字符串")
            out_set = {g2["out"] for g2 in gates}
            for ref in g["inputs"]:
                if ref not in inputs and ref not in out_set:
                    raise PlotSpecError(f"gate 输入引用未知信号：{ref!r}")
    return len(tt), ncols


_CIRCUIT_ELM_TYPES = {
    "V", "Vdc", "Vs", "Source", "BAT", "R", "C", "L", "D", "LED",
    "W", "G", "S", "Rpot", "Lamp", "Fuse", "Zener", "Opamp", "NPN", "PNP",
    "FET_N", "FET_P",
}


def validate_circuit_spec(spec: dict) -> None:
    elements = spec.get("elements")
    if not isinstance(elements, list) or not elements:
        raise PlotSpecError("circuit spec 缺 elements（非空列表）")
    for e in elements:
        if not isinstance(e, dict) or not isinstance(e.get("type"), str):
            raise PlotSpecError("elements 项必须是 {type, at?, to?, label?}")
        if e["type"] not in _CIRCUIT_ELM_TYPES:
            raise PlotSpecError(
                f"circuit 元素类型不支持：{e['type']!r}（白名单 {sorted(_CIRCUIT_ELM_TYPES)}）"
            )
        for key in ("at", "to"):
            val = e.get(key)
            if val is not None and (
                not isinstance(val, (list, tuple)) or len(val) != 2
                or not all(_is_num(v) for v in val)
            ):
                raise PlotSpecError(f"element.{key} 必须是 [x, y] 或省略")


# ---------------------------------------------------------------------------
# 渲染器
# ---------------------------------------------------------------------------

def render_function(spec: dict) -> bytes:
    """函数曲线：numpy 采样 + 白名单求值逐点计算；坐标轴过原点、交点标注。"""
    import numpy as np

    validate_function_spec(spec)
    _setup_cjk()
    xr = spec["xrange"]
    yr = spec.get("yrange")
    xs = np.linspace(xr[0], xr[1], 1200)
    fig, ax = plt.subplots(figsize=(7.2, 5.2))
    for c in spec["curves"]:
        ys = [safe_eval_expr(c["expr"], float(x)) for x in xs]
        ax.plot(xs, ys, lw=1.8, label=c.get("label") or c["expr"])
    for p in spec.get("points") or []:
        ax.scatter([p["x"]], [p["y"]], s=30, zorder=5, color="#b04a38")
        ax.annotate(
            p.get("label") or f"({p['x']:g},{p['y']:g})",
            (p["x"], p["y"]),
            textcoords="offset points", xytext=(7, 6), fontsize=9,
        )
    # 坐标轴（过原点，0 在范围内才可见）
    ax.axhline(0, color="#8a8a8a", lw=0.8, zorder=1)
    ax.axvline(0, color="#8a8a8a", lw=0.8, zorder=1)
    if yr:
        ax.set_ylim(*yr)
    if spec.get("aspect_equal"):
        ax.set_aspect("equal")
    if spec.get("grid", True):
        ax.grid(True, ls="--", alpha=0.35)
    ax.set_xlim(*xr)
    if spec.get("title"):
        ax.set_title(spec["title"], fontsize=11)
    if any(c.get("label") for c in spec["curves"]):
        ax.legend(fontsize=9, frameon=False)
    fig.tight_layout()
    return _fig_bytes(fig)


def render_waveform(spec: dict) -> bytes:
    """数字时序波形：step steps-post 保证高/低电平沿垂直跳变。"""
    n_slots = validate_waveform_spec(spec)
    _setup_cjk()
    signals = spec["signals"]
    n = len(signals)
    fig, ax = plt.subplots(figsize=(max(6.5, n_slots * 0.55 + 2.5), max(2.6, 0.9 * n + 1.4)))
    for i, s in enumerate(signals):
        wave = s["wave"]
        xs = list(range(len(wave) + 1))
        ys = [_WAVE_LEVEL[ch] - 0.5 + i for ch in wave] + [_WAVE_LEVEL[wave[-1]] - 0.5 + i]
        ax.step(xs, ys, where="post", lw=1.8, color=f"C{i}")
        ax.text(-0.55, i, s.get("name") or f"S{i + 1}", ha="right", va="center", fontsize=9.5)
    ax.set_yticks([])
    ax.set_ylim(-0.65, n - 0.35)
    ax.set_xlim(-1.6, n_slots + 0.4)
    ax.set_xticks(range(n_slots + 1))
    for x in range(n_slots + 1):
        ax.axvline(x, color="#c9c2b4", lw=0.6, zorder=0)
    ax.grid(axis="y", ls="--", alpha=0.3)
    if spec.get("title"):
        ax.set_title(spec["title"], fontsize=11)
    fig.tight_layout()
    return _fig_bytes(fig)


_GATE_TYPES = {"AND", "OR", "NOT", "NAND", "NOR", "XOR", "XNOR", "BUFFER"}


def _schemdraw_gate_class(gtype: str):
    from schemdraw import logic

    return {
        "AND": logic.And, "OR": logic.Or, "NOT": logic.Not, "NAND": logic.Nand,
        "NOR": logic.Nor, "XOR": logic.Xor, "XNOR": logic.Xnor, "BUFFER": logic.Buf,
    }[gtype]


def _gate_pin_offsets(n_in: int) -> list[float]:
    """门输入引脚纵向偏移（schemdraw 0.23 真机验证值：2 输入 ±0.25，3 输入 +0.33/0/-0.33）。"""
    if n_in <= 1:
        return [0.0]
    if n_in == 2:
        return [0.25, -0.25]
    if n_in == 3:
        return [0.33, 0.0, -0.33]
    return [(i - (n_in - 1) / 2) * (0.66 / (n_in - 1)) for i in range(n_in)]


def _build_gate_network_png(gates: list[dict], inputs: list[str]) -> bytes:
    """逻辑门网络 → schemdraw PNG（分层左→右布局 + 正交连线）。

    schemdraw 0.23 事实：
    - 门来自 schemdraw.logic（And/Or/Not/Nand/Nor/Xor/Xnor/Buf），
      inputs 是构造参数（logic.And(inputs=3)），不支持链式 .inputs(n)
    - 门放在 (gx, gy) 后直接读 absanchors（in1/in2/in3/out）取引脚坐标，
      连线用 Line().at().to() 画，先水平后垂直
    - PNG 导出用 d.get_imagedata(fmt="png")（Drawing.save 只收文件名）
    布局：深度 = 1 + max(输入引用深度)，x = depth*3；同深度纵向排布。
    """
    import schemdraw
    import schemdraw.elements as elm

    depth_of: dict[str, int] = {}
    for _ in range(len(gates) + 1):  # 迭代到不动点（防环）
        changed = False
        for g in gates:
            refs = g.get("inputs") or []
            d = 1 + max(0 if r in inputs else depth_of.get(r, 0) for r in refs)
            if depth_of.get(g["out"], 0) != d:
                depth_of[g["out"]] = d
                changed = True
        if not changed:
            break
    by_depth: dict[int, list[dict]] = {}
    for g in gates:
        by_depth.setdefault(depth_of[g["out"]], []).append(g)
    pos: dict[str, tuple[float, float]] = {}  # out -> (x, y)
    for depth, gs in by_depth.items():
        for i, g in enumerate(gs):
            pos[g["out"]] = (depth * 3.0, i * 1.8)

    d = schemdraw.Drawing(dpi=160)

    def line(a: tuple[float, float], b: tuple[float, float]):
        nonlocal d
        d += elm.Line().at(a).to(b)

    gate_in: dict[str, list[tuple[float, float]]] = {}
    gate_out: dict[str, tuple[float, float]] = {}
    for g in gates:
        gx, gy = pos[g["out"]]
        n_in = max(1, len(g.get("inputs") or []))
        el = _schemdraw_gate_class(g["type"])(inputs=n_in).at((gx, gy))
        d += el
        anch = getattr(el, "absanchors", None) or {}
        offs = _gate_pin_offsets(n_in)
        ins: list[tuple[float, float]] = []
        for i in range(n_in):
            key = "in" if n_in == 1 else f"in{i + 1}"
            a = anch.get(key)
            ins.append((a.x, a.y) if a is not None else (gx, gy + offs[i]))
        gate_in[g["out"]] = ins
        a = anch.get("out")
        gate_out[g["out"]] = (a.x, a.y) if a is not None else (gx + 1.85, gy)

    for g in gates:
        gx, _ = pos[g["out"]]
        for ref, pin in zip(g.get("inputs") or [], gate_in[g["out"]]):
            if ref in inputs:
                # 输入信号：左侧短 stub + 信号名
                line((gx - 1.2, pin[1]), pin)
                d += elm.Label().at((gx - 1.35, pin[1])).label(ref, loc="left")
            else:
                # 门间连线：源输出 → 水平 → 垂直 → 目标输入（先水平后垂直）
                sx, sy = gate_out[ref]
                line((sx, sy), (pin[0], sy))
                line((pin[0], sy), pin)

    # 最终输出（未被任何门引用的 net）：Dot + 输出名
    referenced = {r for g in gates for r in (g.get("inputs") or [])}
    for g in gates:
        if g["out"] in inputs or g["out"] in referenced:
            continue
        ox, oy = gate_out[g["out"]]
        line((ox, oy), (ox + 0.6, oy))
        d += elm.Dot().at((ox + 0.6, oy))
        d += elm.Label().at((ox + 0.7, oy)).label(g["out"], loc="right")

    return d.get_imagedata(fmt="png")


def render_boolean(spec: dict) -> bytes:
    """布尔题：真值表（matplotlib table）+ 逻辑门网络（schemdraw，可选）。"""
    nrows, ncols = validate_boolean_spec(spec)
    _setup_cjk()
    inputs = spec["inputs"]
    output = spec["output"]
    tt = spec["truth_table"]
    gates = spec.get("gates")
    has_gates = isinstance(gates, list) and len(gates) > 0

    if has_gates:
        gate_png = _build_gate_network_png(gates, inputs)
        fig, (ax_tbl, ax_gate) = plt.subplots(
            1, 2, figsize=(11, max(3.4, 0.9 * nrows + 1.2)),
            gridspec_kw={"width_ratios": [1, 1.25]},
        )
        ax_gate.imshow(plt.imread(io.BytesIO(gate_png)))
        ax_gate.axis("off")
        ax_gate.set_title("逻辑门电路", fontsize=10)
    else:
        fig, ax_tbl = plt.subplots(figsize=(max(4.0, ncols * 1.1 + 1.5), max(2.6, nrows * 0.42 + 1.2)))

    ax_tbl.axis("off")
    tbl = ax_tbl.table(
        cellText=tt, colLabels=[*inputs, output], loc="center", cellLoc="center",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(10)
    tbl.scale(1.0, 1.25)
    ax_tbl.set_title(spec.get("title") or f"真值表：{output}", fontsize=11)
    fig.tight_layout()
    return _fig_bytes(fig)


def _circuit_elm_class(etype: str):
    import schemdraw.elements as elm

    # schemdraw 0.23 事实：Vdc/FetN/FetP 不存在；V/Vdc/Vs/Source 一律 SourceV，
    # FET_N/FET_P → NFet/PFet；BAT → BatteryCell。
    return {
        "V": elm.SourceV, "Vdc": elm.SourceV, "Vs": elm.SourceV, "Source": elm.SourceV,
        "BAT": elm.BatteryCell, "R": elm.Resistor, "C": elm.Capacitor,
        "L": elm.Inductor, "D": elm.Diode, "LED": elm.LED, "W": elm.Line,
        "G": elm.Ground, "S": elm.Switch, "Rpot": elm.Potentiometer,
        "Lamp": elm.Lamp, "Fuse": elm.Fuse, "Zener": elm.Zener,
        "Opamp": elm.Opamp, "NPN": elm.BjtNpn, "PNP": elm.BjtPnp,
        "FET_N": elm.NFet, "FET_P": elm.PFet,
    }[etype]


def render_circuit(spec: dict) -> bytes:
    """电路图：schemdraw 元素序列（坐标网格 + 白名单元素类型）。

    ⚠️ 电源 theta 坑（schemdraw 0.23 源码级根因）：Source 系列 elmparams
    默认 theta=90 会覆盖 to() 自算的角度 → 电源画出来是竖的。修法：所有
    at+to 元素按 at→to 向量自算角度（atan2(dy,dx)）显式传 .theta(ang)；
    Line 无方向语义跳过。PNG 用 d.get_imagedata(fmt="png") 导出。
    """
    import schemdraw

    validate_circuit_spec(spec)
    # schemdraw 0.23 事实：G/Opamp/NPN/PNP/FET 等锚点型元件没有 .to()
    # （两脚件才有），chat 生成的 spec 若带 to 直接忽略，只按 at 放置，
    # 否则抛 "to not defined in Element" 导致整图失败。
    _ANCHOR_ONLY_TYPES = {"G", "Opamp", "NPN", "PNP", "FET_N", "FET_P"}

    d = schemdraw.Drawing(dpi=DPI)
    for e in spec["elements"]:
        el = _circuit_elm_class(e["type"])()
        at = tuple(e["at"]) if e.get("at") else None
        to = tuple(e["to"]) if e.get("to") else None
        if at:
            el.at(at)
        if to and e["type"] not in _ANCHOR_ONLY_TYPES:
            el.to(to)
            if at and e["type"] != "W":
                dx, dy = to[0] - at[0], to[1] - at[1]
                el.theta(math.degrees(math.atan2(dy, dx)))
        if e.get("label"):
            el.label(e["label"])
        d += el
    if spec.get("title"):
        d += schemdraw.elements.Label().at((0.5, -2.0)).label(spec["title"], loc="center")
    return d.get_imagedata(fmt="png")


_RENDERERS: dict[str, Any] = {
    "function": render_function,
    "waveform": render_waveform,
    "boolean": render_boolean,
    "circuit": render_circuit,
}


def render_spec(spec: dict) -> bytes:
    """按 spec['kind'] 分派渲染（同步，供 asyncio.to_thread 包裹）。"""
    kind = spec.get("kind")
    renderer = _RENDERERS.get(kind)
    if renderer is None:
        raise PlotError(f"未知 kind：{kind!r}")
    return renderer(spec)


# ---------------------------------------------------------------------------
# chat 模型转换（非流式）：指令文本 → kind 专属 JSON
# ---------------------------------------------------------------------------

_SCHEMA_EXAMPLES: dict[str, str] = {
    "function": """把用户的绘图指令转换成如下 JSON（只输出 JSON，不要任何其他文字）：
{"curves":[{"expr":"x**2","label":"$y=x^2$"},{"expr":"2*x","label":"$y=2x$"}],"xrange":[-3,3],"yrange":null,"points":[{"x":0,"y":0,"label":"$(0,0)$"},{"x":2,"y":4,"label":"$(2,4)$"}],"title":"$y=x^2$ and $y=2x$","grid":true,"aspect_equal":false}
规则：
- expr 只能含变量 x、运算符 + - * / ** （ ）、以及白名单函数：sin cos tan asin acos atan atan2 sqrt log log10 log2 exp abs floor ceil pi e
- 禁止 import、__、os、getattr 等任何其他写法（会被安全校验拒绝）
- 交点/特殊点放 points
- xrange 必填 [min,max]；yrange 可 null
- ⚠️ title/label 用 matplotlib mathtext 数学排版：数学表达式整体包在 $...$ 里，如
  "$y=x^2$"、"$y=e^{-x^2}$"、"$v_o=-g_m R_L v_{\\pi}$"；上下标必须用 ^ _ 写进 $...$ 内
  （$x^2$、$v_{\\pi}$），希腊字母用 \\pi \\alpha \\omega \\theta 等。
  禁止裸写下划线/尖号文本（y=x^2、r_pi、g_m、v_pi 都是错的，会原样显示下划线）。""",
    "waveform": """把用户的绘图指令转换成如下 JSON（只输出 JSON，不要任何其他文字）：
{"kind":"waveform","title":"2-bit binary counter timing","signals":[{"name":"CLK","wave":"01010101"},{"name":"Q0","wave":"00110011"},{"name":"Q1","wave":"00001111"}]}
规则：
- wave 字符串一个字符 = 一个半周期槽位：0=低电平 1=高电平 p=高脉冲 n=低脉冲 x=不定
- 各信号 wave 长度必须一致；信号名用英文
- title 若含数学量用 matplotlib mathtext：包 $...$，如 "$f_{clk}$ timing"、"$Q_0$/$Q_1$ of 2-bit counter"；
  信号名本身保持简单 ASCII（CLK/Q0/D0），不包 $""",
    "boolean": """把用户的绘图指令转换成如下 JSON（只输出 JSON，不要任何其他文字）：
{"kind":"boolean","expr":"Y=(A·B)+C̄","inputs":["A","B","C"],"output":"Y","truth_table":[[0,0,0,1],[0,0,1,0],[0,1,0,1],[0,1,1,0],[1,0,0,1],[1,0,1,0],[1,1,0,1],[1,1,1,1]],"gates":[{"type":"AND","inputs":["A","B"],"out":"n1"},{"type":"NOT","inputs":["C"],"out":"n0"},{"type":"OR","inputs":["n1","n0"],"out":"Y"}]}
规则：
- truth_table 行序按 inputs[0] 为最高位二进制递增；每行 = 各输入值 + 输出值
- gates 可选：type ∈ AND OR NOT NAND NOR XOR XNOR BUFFER，inputs 引用输入名或其它门 out，out 是唯一名
- 若画真值表即可可不给 gates；给 gates 时同时画门电路与真值表
- 信号名（inputs/output/gates 的 out）保持简单 ASCII 标识符（连线引用键），不包 $；
  title 若含数学量用 mathtext 包 $...$（如 "$Q_0$/$Q_1$ counter"）""",
    "circuit": """把用户的绘图指令转换成如下 JSON（只输出 JSON，不要任何其他文字）：
{"kind":"circuit","title":"PNP BJT hybrid-pi model","elements":[{"type":"V","at":[0,0],"to":[0,3],"label":"$V_{cc}$"},{"type":"R","at":[0,3],"to":[4,3],"label":"$R_C$ = 2 kΩ"},{"type":"PNP","at":[4,3],"label":"Q1"}]}
规则：
- 坐标用任意单位整数网格，串联回路按顺时针排布；to 是元件终点（右/下为正方向）
- type 白名单：V Vdc Vs Source BAT R C L D LED W G S Rpot Lamp Fuse Zener Opamp NPN PNP FET_N FET_P
- ⚠️ label/title 里的数学量用 matplotlib/schemdraw mathtext：包 $...$，下标用 _（$r_{\\pi}$、$g_m$、
  $v_{\\pi}$、$R_C$、$I_B$），希腊字母用 \\pi \\beta \\alpha \\mu \\Omega（如 "$r_{\\pi}$ = 2 kΩ"、
  "$g_m v_{\\pi}$"）。禁止裸写 r_pi / g_m / v_pi / 2 kOhm——会原样显示下划线。""",
}


def _convert_messages(kind: str, text: str) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "你是绘图参数生成器。把用户绘图指令转换为 JSON，"
                "只输出 JSON，不要任何解释、不要 markdown 围栏。\n\n"
                + _SCHEMA_EXAMPLES[kind]
            ),
        },
        {"role": "user", "content": text},
    ]


# ---------------------------------------------------------------------------
# mathtext 兜底 sanitizer：把常见裸写数学量替换成 $...$ mathtext
# 只动 title/label 类展示文本，绝不碰 expr（求值用，改了就废）
# ---------------------------------------------------------------------------

# 整 token 边界：前面不是字母/下划线（允许数字前缀，如 2R_C），
# 后面不是字母/数字/下划线 —— 避免 half_pi / e^{-x^2} 被误切
_LB = r"(?<![A-Za-z_])"
_LA = r"(?![A-Za-z0-9_])"

# (pattern, replacement)：裸 r_pi/g_m/v_o 等下标量 → $r_{\pi}$ 式 mathtext
_MATHTEXT_FIXUPS: list[tuple[str, str]] = [
    # 希腊/高频下标量（电子学）→ 希腊字母 mathtext
    (_LB + r"r_pi" + _LA, r"$r_{\pi}$"),
    (_LB + r"v_pi" + _LA, r"$v_{\pi}$"),
    (_LB + r"g_m" + _LA, r"$g_m$"),
    (_LB + r"r_o" + _LA, r"$r_o$"),
    (_LB + r"v_o" + _LA, r"$v_o$"),
    (_LB + r"v_be" + _LA, r"$v_{be}$"),
    (_LB + r"v_ce" + _LA, r"$v_{ce}$"),
    (_LB + r"i_b" + _LA, r"$i_b$"),
    (_LB + r"i_c" + _LA, r"$i_c$"),
    (_LB + r"i_e" + _LA, r"$i_e$"),
    (_LB + r"V_cc" + _LA, r"$V_{cc}$"),
    (_LB + r"V_be" + _LA, r"$V_{be}$"),
    (_LB + r"V_ce" + _LA, r"$V_{ce}$"),
    # 通用单字母下标 A_B → $A_B$（R_C、I_B、f_clk…）
    (_LB + r"([A-Za-z])_([A-Za-z][A-Za-z0-9]{0,5})" + _LA, None),
    # 裸指数 x^2 / x^-2（整 token；已被花括号包着或后面还有 ^ } 的不碰）
    (_LB + r"([A-Za-z])\^(-?[A-Za-z0-9]+)" + r"(?![\w^}])", None),
    # 单位写法
    (_LB + r"kOhm" + _LA, "kΩ"),
    (_LB + r"Ohm" + _LA, "Ω"),
]
# 字面替换：repl 里的 \pi 等字面反斜杠保持原样 → 函数替换返回字面量
_MATHTEXT_FIXUP_RE = [
    (re.compile(p), (lambda r: lambda _m: r)(repl)) for p, repl in _MATHTEXT_FIXUPS if repl is not None
]
# 组引用替换：显式从 group 组装，避免 \1 不展开 / \pi bad escape 两头翻车。
# 边界排除 $：防把前面字面规则已产出的 $g_m$ 再包一层成 $$g_{m}$$
_MATHTEXT_GROUP_FIXUP_RE = [
    (re.compile(r"(?<![$A-Za-z_])([A-Za-z])_([A-Za-z][A-Za-z0-9]{0,5})(?![A-Za-z0-9_])"),
     lambda m: "$" + m.group(1) + "_{" + m.group(2) + "}$"),
    (re.compile(r"(?<![$A-Za-z])([A-Za-z])\^(-?[A-Za-z0-9]+)(?![\w^}$])"),
     lambda m: "$" + m.group(1) + "^{" + m.group(2) + "}$"),
]


def sanitize_mathtext(text: str) -> str:
    """把 label/title 里常见裸写数学量兜底替换成 mathtext。

    - 已含 $ 的文本不动（模型已按 mathtext 写，避免二次包装/误替换）
    - 只影响展示文本；expr 字段调用方保证不经过这里
    """
    if not isinstance(text, str) or not text or "$" in text:
        return text
    for rx, repl in _MATHTEXT_FIXUP_RE:
        text = rx.sub(repl, text)
    for rx, repl in _MATHTEXT_GROUP_FIXUP_RE:
        text = rx.sub(repl, text)
    return text


def sanitize_spec_labels(spec: dict) -> dict:
    """对 spec 的 title/label 应用 mathtext 兜底；expr 与结构性字段不动。"""
    if not isinstance(spec, dict):
        return spec
    if isinstance(spec.get("title"), str):
        spec["title"] = sanitize_mathtext(spec["title"])
    for c in spec.get("curves") or []:
        if isinstance(c, dict) and isinstance(c.get("label"), str):
            c["label"] = sanitize_mathtext(c["label"])
    for p in spec.get("points") or []:
        if isinstance(p, dict) and isinstance(p.get("label"), str):
            p["label"] = sanitize_mathtext(p["label"])
    for e in spec.get("elements") or []:
        if isinstance(e, dict) and isinstance(e.get("label"), str):
            e["label"] = sanitize_mathtext(e["label"])
    return spec


async def _convert_to_spec(kind: str, text: str) -> dict:
    raw = await chat_complete(_convert_messages(kind, text), temperature=0.1, max_tokens=1600)
    try:
        spec = _parse_json(raw)
    except Exception as e:  # noqa: BLE001 —— 垃圾输出/截断 JSON 一律转 PlotSpecError
        raise PlotSpecError(f"chat 输出不是合法 JSON：{str(e)[:120]}") from e
    if not isinstance(spec, dict):
        raise PlotSpecError("chat 转换未返回 JSON 对象")
    spec.setdefault("kind", kind)
    return sanitize_spec_labels(spec)  # expr 不在此路径内，只兜底 title/label


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

async def generate_plot(instruction: str) -> bytes:
    """解析 [PLOT: ...] 内容 → chat 转 JSON → 校验 → 渲染 PNG bytes。

    - geometry/other → PlotGeometryFallback（上游直接降级 image-model）
    - 转换失败/JSON 非法/表达式注入 → PlotError（上游降级并标注 degraded）
    - 渲染（matplotlib/schemdraw 同步）包 asyncio.to_thread，不阻塞事件循环
    """
    import asyncio

    kind, text = resolve_plot_instruction(instruction)
    if kind in ("geometry", "other"):
        raise PlotGeometryFallback(f"kind={kind} 走 image-model 生图")
    if kind not in _RENDERERS:
        raise PlotError(f"未知 kind：{kind!r}")
    spec = await _convert_to_spec(kind, text)
    spec["kind"] = kind
    return await asyncio.to_thread(render_spec, spec)
