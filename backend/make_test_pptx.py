"""生成 M2 测试用 PPTX（make seed-pptx）：4 页混排。

P1 中文文本页 / P2 英文文本页 / P3 中英混排（公式文本） / P4 整页图片（无文本层 → 触发 vision 读图）
python-pptx 仅开发依赖，不进 requirements.txt（线上传真实课件）。
"""
import sys
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

OUT = Path(__file__).resolve().parent.parent / "data" / "test_sample.pptx"


def add_text_slide(prs, lines, font_size=28):
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    left, top, width, height = Inches(0.6), Inches(0.8), Inches(8.5), Inches(4.8)
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    for i, (text, size, bold) in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = PP_ALIGN.LEFT
        run = p.add_run()
        run.text = text
        run.font.size = Pt(size or font_size)
        run.font.bold = bold
    return slide


def add_picture_slide(prs):
    """整页图片：用 python-pptx 画不出位图，改为插入一张程序生成的 PNG。"""
    import struct
    import zlib

    # 手写最小 PNG（256x144 纯色 + 无文字）——图片页：PDF 化后无文本层
    width, height = 256, 144
    raw = b""
    row = b"\x00" + bytes([120, 140, 180] * width)  # filter byte + RGB
    raw = row * height

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_picture_from_bytes = None  # noqa: 删除误属性，走下方式
    import io

    slide.shapes.add_picture(io.BytesIO(png), 0, 0, Inches(10), Inches(5.625))
    return slide


def main() -> int:
    prs = Presentation()
    prs.slide_width = Inches(10)
    prs.slide_height = Inches(5.625)

    # P1 中文页
    add_text_slide(prs, [
        ("第一章 极限与连续", 32, True),
        ("极限描述函数在某个变化过程中的趋势。", 22, False),
        ("若 x→a 时 f(x) 无限接近 L，则记作 lim f(x)=L。", 22, False),
        ("中文页面测试：本页应检测为 zh。", 20, False),
    ])
    # P2 英文页
    add_text_slide(prs, [
        ("Limits and Continuity", 32, True),
        ("A function f is continuous at a if lim f(x) = f(a).", 22, False),
        ("English page test: this slide should be detected as en.", 20, False),
        ("The epsilon-delta definition formalizes the idea of closeness.", 20, False),
    ])
    # P3 中英混排 + 公式
    add_text_slide(prs, [
        ("Important Formula / 重要公式", 28, True),
        ("Derivative definition: f'(x) = lim_{h->0} [f(x+h) - f(x)] / h", 22, False),
        ("导数的定义：差商的极限。混合页面 mixed page test 2026。", 22, False),
    ])
    # P4 整页图片（无文本层 → empty → vision 读图）
    add_picture_slide(prs)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    prs.save(OUT)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, 4 slides)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
