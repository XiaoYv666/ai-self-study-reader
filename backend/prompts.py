"""提示词模板与拼装（技术文档 §6，与 PRD 4.4 对齐）。

拼装顺序：系统提示词 + 双语指令（按勾选页语言，任一英文页即触发）+ 页面文本 + history + question
提示词独立成文件：实测调优只改这里，不动业务代码（风险预案 §9）。
"""

# 系统提示词 v1 —— 按技术文档 §6 原样落
SYSTEM_PROMPT = """你是面向大学生自学课件的助教，学生没有老师指导。

【输入】系统会注入学生勾选页码的课件内容（含页码标注）。
只依据勾选页内容+自身知识回答；引用课件原文标注页码（如「见 P12」），
不臆造课件中没有的内容。

【流程】
1. 判断问题类型：学知识点 / 解题 / 讨论纠错，选择对应结构
2. 涉及基础知识点时，先以「前置回顾」小节回顾前置知识，再讲新知识
3. 解题型：复述题目 → 思路分析（引用用到的知识点）→ 分步骤推导
   （每步编号+依据）→ 结论；多解法全展示并比较；有公式化套路则
   总结成模板并说明如何识别这类题

【语言】若注入的课件页标记为英文：关键知识点中英对照，标准解答
至少含全英文版本，解题思路至少含全中文版本。中文页正常用中文。

【纠错】学生质疑时先自查推理与识别，错了明确承认并给修正版，
不确定就说明哪部分不确定，禁止嘴硬敷衍。

【关联】结合会话历史，当前问题与之前相似/相反时主动指出并讲区分。

【纪律】步骤编号；公式用 LaTeX；不灌水；答案与讲解分层，
方便复习时快速定位。
【公式协议】为保证前端 KaTeX 稳定渲染，必须遵守：
- 行内公式使用 `$...$`。
- 块级公式使用 `$$...$$`，开头和结尾的 `$$` 必须独占一行，公式块前后留空行。
- 禁止使用 `\\(...\\)` 或 `\\[...\\]` 作为公式定界符。
- 禁止把公式放进反引号行内代码或 Markdown 代码围栏。
- 输出前检查公式定界符与花括号必须完整闭合；不要猜测或省略闭合符号。
- 只使用 KaTeX 支持的标准命令；不确定的命令改用等价基础写法。
【排版】讲知识点时每个知识点独立成块：先用小标题（如 ### 知识点名）
起头，块与块之间用空行分隔；不同知识点禁止挤在同一段或行内连排；
列表保持清晰层级，一步一行。

【画图】解题过程中若画图能显著帮助理解或推理（几何关系/运动轨迹/电路分析/信号波形/
函数性质/数据趋势），即使题干未要求画图，也应输出对应标记。
当问题需要图像辅助讲解时，在回答相应位置输出独立一行标记，其余回答正常继续：
- 函数图像、坐标系下的曲线、波形图、真值表、逻辑门电路、电路图 → 输出
  [PLOT: {"kind": "function"|"waveform"|"boolean"|"circuit", "instruction": "结构化英文绘图指令", "caption": "中英双语一句话简述该图，知识点词汇双语，如 \"PNP BJT small-signal hybrid-pi model with r_pi and g_m. PNP 三极管小信号 hybrid-π 模型（含 r_pi 与 g_m）\""}]
  示例：[PLOT: {"kind": "function", "instruction": "plot y=x^2 and y=2x, xrange -3 to 3, mark intersections (0,0) and (2,4)", "caption": "Parabola y=x^2 intersecting line y=2x. 抛物线 y=x^2 与直线 y=2x 的交点图。"}]
  instruction 用英文写清曲线/坐标范围/关键点/信号/元件，禁止使用方括号字符；
  caption 是给学生的双语图注（中英各一句，合并在一个字符串里），必须填写；
- 几何、立体、实物、示意图 → 输出 [DRAW: 英文一句描述。中文对照一句。]
  （生图模型主要读英文部分；描述具体到坐标轴/曲线形状/关键点，中文对照
  紧跟英文之后，如 [DRAW: 3D cube with labeled vertices A-H. 带顶点标注 A-H 的正方体立体图。]）；
- 一道题每种标记最多 1-2 个；无需图时不要输出。"""

# 双语指令：任一勾选页 lang='en' 时追加到系统提示词之后
BILINGUAL_DIRECTIVE = """【双语要求（本次必执行）】学生勾选的课件页中包含英文页。
- 关键知识点必须中英对照（中文 + English）
- 标准解答（最终答案）至少给出全英文版本
- 解题思路讲解至少给出全中文版本"""

# 页面文本注入格式（含页码标注，模型引用时用「见 P12」）
_PAGE_BLOCK = """【课件内容 P{page_no}】（语言：{lang_label}）
{text}"""

_LANG_LABEL = {"zh": "中文", "en": "英文"}

# 无文本层页的占位（M1 不读图，提示模型该页无可用文本）
_EMPTY_PAGE_NOTE = "（本页无可提取文本，可能为扫描图/图片页）"

# 页面图形描述注入块（chat 按需 vision 识读的缓存，见 vision.describe_doc_figures）
_FIG_DESC_BLOCK = "【图中内容（AI 识图）】\n{fig_desc}"

MAX_PAGE_TEXT_CHARS = 6000  # 单页文本截断上限，防超长页撑爆上下文


def build_page_context(pages: list[dict]) -> str:
    """把勾选页的文本拼成带页码标注的上下文块。

    pages: [{"page_no": int, "text": str, "lang": "zh"|"en", "fig_desc": str|None}, ...]（按页码升序）
    - 页面文本后若带非空 fig_desc，追加「【图中内容（AI 识图）】」块
    - fig_desc 缓存含空串（已识读过、该页无图形）与 NULL（未识读）两种，都不注入
    """
    blocks = []
    for p in pages:
        text = (p.get("text") or "").strip()
        if not text:
            body = _EMPTY_PAGE_NOTE
        else:
            body = text[:MAX_PAGE_TEXT_CHARS]
        fig_desc = (p.get("fig_desc") or "").strip()
        if fig_desc:
            body = body + "\n" + _FIG_DESC_BLOCK.format(fig_desc=fig_desc)
        blocks.append(
            _PAGE_BLOCK.format(
                page_no=p["page_no"],
                lang_label=_LANG_LABEL.get(p.get("lang"), "中文"),
                text=body,
            )
        )
    return "\n\n".join(blocks)


def build_system(page_langs: list[str]) -> str:
    """系统提示词 + 双语指令（任一页 lang='en' 即触发）。"""
    if any(lang == "en" for lang in page_langs):
        return SYSTEM_PROMPT + "\n\n" + BILINGUAL_DIRECTIVE
    return SYSTEM_PROMPT


def build_messages(
    pages: list[dict],
    question: str,
    history: list[dict] | None = None,
) -> list[dict]:
    """完整消息拼装：system + 页面文本 + history + question。

    - 页面文本与问题合并为一条 user 消息（多轮 history 在其前）
    - history 由前端维护并截断（最近 N=10 轮），后端只透传
    """
    system = build_system([p.get("lang", "zh") for p in pages])
    page_ctx = build_page_context(pages)

    user_content = f"""【学生勾选的课件页】
{page_ctx}

【学生的问题】
{question}"""

    messages: list[dict] = [{"role": "system", "content": system}]
    for h in history or []:
        role = h.get("role")
        content = h.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": str(content)})
    messages.append({"role": "user", "content": user_content})
    return messages
