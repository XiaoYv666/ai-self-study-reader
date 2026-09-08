"""AI 回答的 KaTeX/Markdown 数学格式协议测试。"""
import unittest

from prompts import SYSTEM_PROMPT, build_system


class PromptMathProtocolTest(unittest.TestCase):
    def test_system_prompt_requires_renderer_safe_math_delimiters(self):
        required = [
            "行内公式使用 `$...$`",
            "块级公式使用 `$$...$$`",
            "`$$` 必须独占一行",
            "禁止使用 `\\(...\\)`",
            "`\\[...\\]`",
            "反引号",
            "代码围栏",
            "定界符与花括号必须完整闭合",
            "KaTeX 支持的标准命令",
        ]
        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, SYSTEM_PROMPT)

    def test_bilingual_system_keeps_math_protocol(self):
        system = build_system(["en"])
        self.assertIn("行内公式使用 `$...$`", system)
        self.assertIn("块级公式使用 `$$...$$`", system)


if __name__ == "__main__":
    unittest.main()
