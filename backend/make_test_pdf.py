"""生成 8 页中英混排测试 PDF → data/test_sample.pdf

页面构成（覆盖 M1 全部检测路径）：
  P1 中文封面  P2-3 中文知识点  P4-5 英文讲义页（触发 lang='en' + 双语指令）
  P6 中英混排（偏中文）  P7 英文例题页  P8 图片页（无文本层 → empty_pages）
"""
from pathlib import Path

import pymupdf

OUT = Path(__file__).resolve().parent.parent / "data" / "test_sample.pdf"

ZH_BODY = """第一章 极限与连续

1.1 极限的定义
设函数 f(x) 在点 x0 的某个去心邻域内有定义。
如果当 x 无限趋近于 x0 时，f(x) 无限趋近于某个确定的常数 A，
则称 A 为 f(x) 当 x→x0 时的极限，记作 lim f(x) = A。

1.2 连续的定义
若函数在一点处左极限、右极限都存在且等于该点函数值，
则函数在该点连续。连续函数在闭区间上有界且取得最大最小值。

1.3 重要极限
lim (sin x / x) = 1，其中 x 趋近于零。
这个结论在求未定式极限时非常常用，配合等价无穷小替换可以简化计算。
两个重要极限是整个极限理论计算的基础工具。
"""

EN_BODY = """Chapter 3: Derivatives and Differentiation Rules

3.1 Definition of the Derivative
The derivative of a function f at a point x is defined as the limit
of the difference quotient as h approaches zero. Geometrically, the
derivative represents the slope of the tangent line to the curve at
that point. When the limit exists, we say the function is
differentiable there. Differentiability implies continuity, but the
converse is not true in general.

3.2 Rules of Differentiation
The product rule states that the derivative of a product of two
differentiable functions equals the first times the derivative of
the second plus the second times the derivative of the first. The
quotient rule handles ratios of functions. The chain rule allows us
to differentiate composite functions by multiplying derivatives of
outer and inner functions respectively.
"""

EN_PROBLEM = """Exercise: Related Rates Problem

A ladder 10 feet long rests against a vertical wall. Suppose the
bottom of the ladder slides away from the wall at a rate of one foot
per second. How fast is the top of the ladder sliding down the wall
when the bottom of the ladder is six feet from the wall?

Solution sketch: Let x denote the distance from the wall to the
bottom, and y the height of the top. We have x squared plus y
squared equals one hundred. Differentiate both sides with respect
to time, then substitute the given values to solve for dy/dt.
"""

MIXED_BODY = """2.4 泰勒定理 (Taylor's Theorem)

若函数 f 在含有 x0 的开区间内具有 n+1 阶导数，则可以用 n 次
泰勒多项式 (Taylor polynomial) 近似表示：误差项称为拉格朗日
余项 (Lagrange remainder)。常见展开如 e^x、sin x、cos x、
ln(1+x) 都属于基本初等函数的麦克劳林级数 (Maclaurin series)。
做题时先展开到合适阶数，再代入具体点求近似值即可。
"""


def main() -> None:
    doc = pymupdf.open()
    specs = [
        ("《高等数学》讲义\n\n主讲：张老师\n\n第一章 极限与连续", 22),
        (ZH_BODY, 12),
        (ZH_BODY.replace("第一章", "第一章（续）") + "\n\n例 1：求 lim (x²−1)/(x−1)，x→1。\n解：分子分母同时因式分解后约去零因子，得到极限值为 2。", 12),
        (EN_BODY, 12),
        (EN_BODY.replace("3.2", "3.3 Applications"), 12),
        (MIXED_BODY, 12),
        (EN_PROBLEM, 12),
        ("", 12),  # 图片页：仅插图无文本 → empty
    ]
    for i, (body, size) in enumerate(specs, start=1):
        page = doc.new_page()  # 默认 A4: 595x842pt
        if i == 8:  # 画个简单图形模拟扫描/图片页
            rect = pymupdf.Rect(150, 250, 445, 545)
            page.draw_rect(rect, color=(0.2, 0.2, 0.7), width=2)
            page.draw_line(pymupdf.Point(150, 250), pymupdf.Point(445, 545), color=(0.7, 0.2, 0.2), width=1.5)
            page.draw_circle(pymupdf.Point(297.5, 397.5), 80, color=(0.2, 0.6, 0.2), width=1.5)
            continue
        page.insert_textbox(pymupdf.Rect(60, 60, 535, 780), body, fontsize=size,
                            fontname="china-s" if any("\u4e00" <= c <= "\u9fff" for c in body) else "helv",
                            align=0)
    doc.save(str(OUT))
    print(f"OK -> {OUT}  pages=8  (P4,P5,P7 英文页；P6 中英混排偏中文；P8 无文本层)")


if __name__ == "__main__":
    main()
