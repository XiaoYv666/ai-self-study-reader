// Markdown 渲染器（V2：react-markdown + remark-math + rehype-katex，公式真渲染）
// 导出接口不变：export function Markdown({ text }: { text: string })
// code/pre/table/blockquote 等元素由 index.css 中现有 .md 系列样式直接覆盖（react-markdown 输出标准语义标签）。
// 流式安全与模型兼容：代码区域外统一 LaTeX 定界符，并暂时隐藏流式尾部未闭合公式。
import ReactMarkdown from 'react-markdown'
import remarkMath from 'remark-math'
import remarkGfm from 'remark-gfm'
import rehypeKatex from 'rehype-katex'
import { normalizeMathMarkdown } from './mathMarkdown'

export function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkMath, remarkGfm]}
        rehypePlugins={[[rehypeKatex, { throwOnError: false, strict: 'ignore', errorColor: '#6d6150' }]]}
      >
        {normalizeMathMarkdown(text)}
      </ReactMarkdown>
    </div>
  )
}
