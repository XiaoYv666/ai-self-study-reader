"""PyMuPDF 逐页抽取 + 语言检测。

- 每页抽文本、记录实际宽高（pt）——前端按 width/height 比例渲染页面卡（§4.1）
- 语言检测：词频比例阈值（技术文档 §1 选型：自写 ~20 行，只区分中/英页）
- 检测逻辑独立成函数 detect_lang()，便于阈值调优（风险预案 §9：误判时只改这一处）
"""
import re
from dataclasses import dataclass

import pymupdf  # PyMuPDF 新 API；`import fitz` 已弃用

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_EN_WORD_RE = re.compile(r"[A-Za-z]+")
# 过滤掉 LaTeX/代码类噪声 token（如 \frac、xyz 单字母变量）后再计英文词频
_EN_TOKEN_RE = re.compile(r"[A-Za-z]{2,}")

# 页内图片检测阈值（pt/px 均按图像像素宽高算）：宽或高 ≥ 100px 的位图才算「内容图」。
# 校徽/logo/装饰小图标一般小于 100px 宽被跳过；宁可多识不可漏——课件 logo 偏大导致
# 误标也接受（代价只是多一次 vision 识读，vision 会回「无图形」并缓存）。
IMAGE_MIN_PX = 100


@dataclass
class PageData:
    page_no: int      # 1-based
    text: str
    lang: str         # 'zh' | 'en'
    width: float      # pt
    height: float     # pt
    source: str = "text"  # 'text' | 'vision'（M2 读图回填后改写）
    has_image: int = 0    # 0|1：页内是否有 ≥100px 的嵌入位图（物理检测，非 LLM 判断）


def detect_lang(text: str, en_ratio_threshold: float = 0.65) -> str:
    """中/英页检测：词频比例阈值。

    规则（阈值集中在此，调优只改这里）：
    - 无有效文本 → 'zh'（默认；空页由调用方标记 empty）
    - 英文有效词数 / (英文词数 + 中文字符数) ≥ 阈值 → 'en'
    - 否则 → 'zh'（中英混排页偏中文处理，符合「中文页正常用中文」）
    """
    if not text or not text.strip():
        return "zh"
    cjk_chars = len(_CJK_RE.findall(text))
    en_words = len(_EN_TOKEN_RE.findall(text))
    total = cjk_chars + en_words
    if total == 0:
        return "zh"
    return "en" if en_words / total >= en_ratio_threshold else "zh"


def page_has_image(page: "pymupdf.Page") -> int:
    """物理检测：页内是否嵌入 ≥100px 的内容位图。返回 1/0。

    - page.get_images(full=True) 列出页内引用的全部嵌入图像（xref, ..., width, height, ...）
    - 只看像素宽高（下标 2/3），宽或高 ≥ IMAGE_MIN_PX 即算内容图
    - 这是 PDF 结构层面的事实信息（上传时即知），替代此前「LLM 猜页面是否有图」的预判
    """
    for im in page.get_images(full=True):
        try:
            w, h = int(im[2]), int(im[3])
        except (TypeError, ValueError, IndexError):
            continue
        if w >= IMAGE_MIN_PX or h >= IMAGE_MIN_PX:
            return 1
    return 0


def extract_pdf(pdf_path: str) -> list[PageData]:
    """逐页抽取：文本 + 宽高(pt) + 语言标记 + 页内图片物理检测。1-based 页码与课件页码对齐。"""
    pages: list[PageData] = []
    with pymupdf.open(pdf_path) as doc:
        for i, page in enumerate(doc):
            text = page.get_text("text").strip()
            rect = page.rect
            pages.append(
                PageData(
                    page_no=i + 1,
                    text=text,
                    lang=detect_lang(text),
                    width=round(rect.width, 2),
                    height=round(rect.height, 2),
                    has_image=page_has_image(page),
                )
            )
    return pages
