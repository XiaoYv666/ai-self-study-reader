import assert from 'node:assert/strict'
import test from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ReactMarkdown from 'react-markdown'
import rehypeKatex from 'rehype-katex'
import remarkMath from 'remark-math'

import { normalizeMathMarkdown } from '../src/lib/mathMarkdown.ts'

const normalize = (text) => normalizeMathMarkdown(text)

function renderMath(text) {
  return renderToStaticMarkup(
    createElement(
      ReactMarkdown,
      {
        remarkPlugins: [remarkMath],
        rehypePlugins: [[rehypeKatex, { throwOnError: false, strict: 'ignore', errorColor: '#6d6150' }]],
      },
      normalize(text),
    ),
  )
}

test('keeps standard inline and display math unchanged', () => {
  assert.equal(normalize('行内 $x^2$ 继续。'), '行内 $x^2$ 继续。')
  assert.equal(normalize('前文\n\n$$\nx^2\n$$\n\n后文'), '前文\n\n$$\nx^2\n$$\n\n后文')
})

test('normalizes parenthesis and bracket LaTeX delimiters outside code', () => {
  assert.equal(normalize('中文 \\(x^2\\) 混排'), '中文 $x^2$ 混排')
  assert.equal(normalize('前文\\[x^2\\]后文'), '前文\n\n$$\nx^2\n$$\n\n后文')
})

test('does not normalize delimiters in inline or fenced code', () => {
  const source = '示例 `\\(x^2\\)`\n\n```tex\n\\[x^2\\]\n\n\nkeep gaps\n```\n\n正文 \\(y\\)'
  const expected = '示例 `\\(x^2\\)`\n\n```tex\n\\[x^2\\]\n\n\nkeep gaps\n```\n\n正文 $y$'
  assert.equal(normalize(source), expected)
})

test('removes invalid model escapes before comparison symbols outside code', () => {
  const source = '正文 \\= \\< \\> \\≤ \\≥ \\≠；公式 $a \\= b, c \\≤ d, e \\≠ f$。'
  const expected = '正文 = < > ≤ ≥ ≠；公式 $a = b, c ≤ d, e ≠ f$。'
  assert.equal(normalize(source), expected)
})

test('keeps invalid-looking comparison escapes inside inline and fenced code', () => {
  const source = '命令 `value \\= other`\n\n```text\na \\≤ b\nc \\> d\n```\n\n正文 x \\≥ y'
  const expected = '命令 `value \\= other`\n\n```text\na \\≤ b\nc \\> d\n```\n\n正文 x ≥ y'
  assert.equal(normalize(source), expected)
})

test('keeps legal LaTeX commands and deliberately doubled backslashes unchanged', () => {
  const source = '$a \\le b, c \\ge d, e \\neq f, \\frac{1}{2}$ and \\\\= literal'
  assert.equal(normalize(source), source)
})

test('temporarily hides an unfinished inline formula and restores it when closed', () => {
  assert.equal(normalize('已知 $x^2 + 1'), '已知 ')
  assert.equal(normalize('第一行已知 $x^2 +\n1'), '第一行已知 ')
  assert.equal(normalize('已知 $x^2 + 1$。'), '已知 $x^2 + 1$。')
})

test('temporarily hides an unfinished display formula and restores it when closed', () => {
  assert.equal(normalize('推导：\n$$\nx^2 + 1'), '推导：\n')
  assert.equal(normalize('推导：\n$$\nx^2 + 1\n$$\n结论'), '推导：\n\n$$\nx^2 + 1\n$$\n\n结论')
})

test('escaped dollars and currency are not mistaken for unfinished math', () => {
  assert.equal(normalize('价格是 $10，折后 $8。'), '价格是 $10，折后 $8。')
  assert.equal(normalize('输入 \\$HOME 或支付 \\$10。'), '输入 \\$HOME 或支付 \\$10。')
})

test('handles multiple formulas in Chinese prose without mixing dollar delimiter widths', () => {
  const source = '若 \\(a+b\\) 成立，则 $c=d$。\n\n\\[a^2+b^2=c^2\\]\n\n最后是 $e=f$。'
  const expected = '若 $a+b$ 成立，则 $c=d$。\n\n$$\na^2+b^2=c^2\n$$\n\n最后是 $e=f$。'
  assert.equal(normalize(source), expected)
})

test('keeps unsupported or malformed closed KaTeX source readable for the renderer', () => {
  assert.equal(normalize('公式 $\\notARealKatexCommand{x}$ 仍应保留。'), '公式 $\\notARealKatexCommand{x}$ 仍应保留。')
  assert.equal(normalize('公式 $\\frac{1}{$'), '公式 $\\frac{1}{$')
})

test('normalized legacy delimiters produce KaTeX markup in the real render pipeline', async () => {
  const html = await renderMath('行内 \\(x^2\\)\n\n块级 \\[y^2\\]')
  assert.match(html, /class="katex"/)
  assert.match(html, /class="katex-display"/)
  assert.doesNotMatch(html, /\\\(|\\\[/)
})

test('invalid escaped comparison symbols render without exposed backslashes or KaTeX errors', async () => {
  const html = await renderMath('正文 \\≤；公式 $a \\= b$ 与 $c \\≥ d$')
  assert.doesNotMatch(html, /\\[=≤≥≠<>]/)
  assert.doesNotMatch(html, /katex-error/)
  assert.match(html, /class="katex"/)
})

test('unsupported KaTeX commands fall back to readable source instead of throwing', async () => {
  const html = await renderMath('公式 $\\notARealKatexCommand{x}$。')
  assert.match(html, /notARealKatexCommand/)
  assert.match(html, /#6d6150/)
  assert.doesNotMatch(html, /#cc0000/)
})
