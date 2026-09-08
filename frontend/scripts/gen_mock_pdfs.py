#!/usr/bin/env python3
"""生成 M1 演示用的 mock 课件 PDF（纯 stdlib 手写 PDF，无第三方依赖）。

6 份课件均为 960x540（16:9 幻灯片比例，与前端 M1 的 16:9 渲染约定一致）：
  green.pdf   格林公式与曲线积分   14 页
  taylor.pdf  泰勒展开与中值定理   12 页
  eigen.pdf   特征值与对角化       16 页
  vspace.pdf  向量空间与线性变换   10 页
  emag.pdf    电磁感应与麦克斯韦方程组 20 页
  thermo.pdf  热力学基础           15 页

页面内容为英文占位文本（Helvetica 标准字体，无需嵌入），
页面标题/序号可帮助人工核对连续滚动与页码勾选逻辑。
"""
import os

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "public", "mock")

BOOKS = [
    ("green.pdf", "Green's Theorem & Line Integrals", "Multivariable Calculus - Chapter 10", 14),
    ("taylor.pdf", "Taylor Expansion & Mean Value Theorems", "Calculus I - Chapter 3", 12),
    ("eigen.pdf", "Eigenvalues and Diagonalization", "Linear Algebra - Chapter 5", 16),
    ("vspace.pdf", "Vector Spaces & Linear Transformations", "Linear Algebra - Chapter 1", 10),
    ("emag.pdf", "Electromagnetic Induction & Maxwell's Equations", "University Physics - Chapter 12", 20),
    ("thermo.pdf", "Fundamentals of Thermodynamics", "University Physics - Chapter 8", 15),
]

BULLETS = [
    "Review: key concepts and definitions from the previous lecture",
    "Theorem statement, intuition, and geometric meaning",
    "Worked example: step-by-step solution with derivations",
    "Common pitfalls and exam tips for this topic",
    "Bilingual glossary: zh / en terms used in this section",
    "Summary of this section and connection to the next",
]


def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def page_content_stream(idx: int, total: int, title: str, subtitle: str) -> str:
    kicker = f"LECTURE {idx} / {total}  -  {subtitle.upper()}"
    bullets = [BULLETS[(idx + i) % len(BULLETS)] for i in range(3)]
    parts = [
        "0.984 0.969 0.925 rg 0 0 960 540 re f",            # 米白纸底
        "0.643 0.408 0.227 rg 0 532 960 8 re f",            # 顶部暖棕细条
        "0.91 0.88 0.82 rg 0 0 960 24 re f",                # 底部淡条
        f"BT /F2 13 Tf 0.62 0.56 0.45 rg 64 488 Td ({esc(kicker)}) Tj ET",
        f"BT /F2 30 Tf 0.235 0.20 0.15 rg 64 428 Td ({esc(title)}) Tj ET",
        "0.643 0.408 0.227 rg 64 402 120 3 re f",           # 标题下暖棕短横线
        f"BT /F1 15 Tf 0.43 0.38 0.31 rg 64 372 Td (Slide {idx} of {total}) Tj ET",
        f"BT /F1 17 Tf 0.30 0.26 0.19 rg 64 312 Td ({(idx * 37) % 89 + 10}. {esc(bullets[0])}) Tj ET",
        f"BT /F1 17 Tf 0.30 0.26 0.19 rg 64 278 Td ({(idx * 53) % 79 + 10}. {esc(bullets[1])}) Tj ET",
        f"BT /F1 17 Tf 0.30 0.26 0.19 rg 64 244 Td ({(idx * 71) % 69 + 10}. {esc(bullets[2])}) Tj ET",
        "0.94 0.91 0.85 rg 64 100 832 90 re f",             # 提示卡底
        "0.643 0.408 0.227 rg 64 100 4 90 re f",            # 提示卡左边条
        f"BT /F1 14 Tf 0.43 0.38 0.31 rg 82 156 Td (Tip: select this page and ask the AI tutor about it.) Tj ET",
        f"BT /F1 11 Tf 0.62 0.56 0.45 rg 64 36 Td (Page {idx} of {total}  -  Bilingual Courseware) Tj ET",
    ]
    return "\n".join(parts)


def build_pdf(path: str, title: str, subtitle: str, total: int) -> None:
    objs: list[str] = ["0"]  # 1-indexed

    # 1 catalog
    objs.append("<< /Type /Catalog /Pages 2 0 R >>")
    # 2 pages tree
    kids = " ".join(f"{3 + i} 0 R" for i in range(total))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {total} >>")
    # 3..2+total page objects
    for i in range(total):
        objs.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 960 540] "
            "/Resources << /Font << /F1 100 0 R /F2 101 0 R >> >> "
            f"/Contents {103 + i} 0 R >>"
        )
    # font objects
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")       # 100
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")  # 101
    # content streams
    for i in range(total):
        data = page_content_stream(i + 1, total, title, subtitle)
        objs.append(f"<< /Length {len(data.encode('latin-1'))} >>\nstream\n{data}\nendstream")

    body = bytearray()
    offsets = [0]
    for o in objs[1:]:
        offsets.append(len(body))
        body += f"{len(offsets) - 1} 0 obj\n{o}\nendobj\n".encode("latin-1")

    xref_pos = len(body)
    body += f"xref\n0 {len(objs)}\n".encode("latin-1")
    body += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        body += f"{off:010d} 00000 n \n".encode("latin-1")
    body += (
        f"trailer\n<< /Size {len(objs)} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("latin-1")
    )

    with open(path, "wb") as f:
        f.write(body)
    print(f"  {os.path.basename(path)}: {total} pages, {len(body)} bytes")


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    for fname, title, subtitle, total in BOOKS:
        build_pdf(os.path.join(OUT_DIR, fname), title, subtitle, total)
    print(f"done -> {OUT_DIR}")


if __name__ == "__main__":
    main()
