type Segment = { code: boolean; value: string }

function isEscaped(source: string, index: number): boolean {
  let slashes = 0
  for (let i = index - 1; i >= 0 && source[i] === '\\'; i--) slashes++
  return slashes % 2 === 1
}

function splitCodeSegments(source: string): Segment[] {
  const segments: Segment[] = []
  let plainStart = 0
  let i = 0

  const pushPlain = (end: number) => {
    if (end > plainStart) segments.push({ code: false, value: source.slice(plainStart, end) })
  }

  while (i < source.length) {
    const lineStart = i === 0 || source[i - 1] === '\n'
    if (lineStart) {
      const fence = source.slice(i).match(/^( {0,3})(`{3,}|~{3,})[^\n]*(?:\n|$)/)
      if (fence) {
        pushPlain(i)
        const marker = fence[2][0]
        const minimum = fence[2].length
        let end = i + fence[0].length
        const closeRe = new RegExp(`^ {0,3}${marker}{${minimum},}[ \\t]*(?:\\n|$)`)
        while (end < source.length) {
          const close = source.slice(end).match(closeRe)
          if (close) {
            end += close[0].length
            break
          }
          const newline = source.indexOf('\n', end)
          end = newline === -1 ? source.length : newline + 1
        }
        segments.push({ code: true, value: source.slice(i, end) })
        i = end
        plainStart = end
        continue
      }
    }

    if (source[i] === '`' && !isEscaped(source, i)) {
      let ticks = 1
      while (source[i + ticks] === '`') ticks++
      const marker = '`'.repeat(ticks)
      const close = source.indexOf(marker, i + ticks)
      if (close !== -1) {
        pushPlain(i)
        const end = close + ticks
        segments.push({ code: true, value: source.slice(i, end) })
        i = end
        plainStart = end
        continue
      }
    }
    i++
  }

  pushPlain(source.length)
  return segments
}

function removeInvalidComparisonEscapes(source: string): string {
  return source.replace(/\\([=<>≤≥≠])/g, (match, symbol: string, offset: number) =>
    isEscaped(source, offset) ? match : symbol,
  )
}

function replaceLegacyDelimiters(source: string): string {
  const inline = source.replace(/\\\(([\s\S]*?)\\\)/g, (match, body: string, offset: number) =>
    isEscaped(source, offset) ? match : `$${body}$`,
  )

  return inline.replace(/\\\[([\s\S]*?)\\\]/g, (match, body: string, offset: number) => {
    if (isEscaped(inline, offset)) return match
    return `\n\n$$\n${body.trim()}\n$$\n\n`
  })
}

function normalizeDisplaySpacing(source: string): string {
  const segments = splitCodeSegments(source)
  const normalized: Segment[] = []
  for (const segment of segments) {
    const value = segment.code
      ? segment.value
      : segment.value.replace(/(^|[^$])\$\$[ \t]*\n?([\s\S]*?)\n?[ \t]*\$\$(?!\$)/g, (_match, prefix: string, body: string) => {
          const before = prefix === '\n' ? '\n\n' : `${prefix}\n\n`
          return `${before}$$\n${body.trim()}\n$$\n\n`
        })
    const previous = normalized[normalized.length - 1]
    if (previous && !previous.code && !segment.code) previous.value += value
    else normalized.push({ code: segment.code, value })
  }
  return normalized.map((segment) => (segment.code ? segment.value : segment.value.replace(/\n{3,}/g, '\n\n'))).join('')
}

function guardUnfinishedDollarMath(source: string): string {
  const segments = splitCodeSegments(source)
  let offset = 0
  let mode: 'inline' | 'display' | null = null
  let opener = -1

  for (const segment of segments) {
    if (segment.code) {
      offset += segment.value.length
      continue
    }

    const text = segment.value
    for (let i = 0; i < text.length; i++) {
      if (text[i] !== '$' || isEscaped(text, i)) continue
      const absolute = offset + i
      const pair = text[i + 1] === '$'

      if (mode === 'display') {
        if (pair) {
          mode = null
          opener = -1
          i++
        }
        continue
      }

      if (mode === 'inline') {
        if (!pair) {
          mode = null
          opener = -1
        } else {
          i++
        }
        continue
      }

      if (pair) {
        mode = 'display'
        opener = absolute
        i++
        continue
      }

      const next = text[i + 1]
      // remark-math does not safely distinguish an unfinished formula from currency.
      // Treat "$" followed by a digit as ordinary currency unless a closing delimiter exists.
      if (!next || /\s/.test(next) || /\d/.test(next)) continue
      mode = 'inline'
      opener = absolute
    }
    offset += text.length
  }

  return mode && opener >= 0 ? source.slice(0, opener) : source
}

/**
 * Normalizes common model math delimiters without touching Markdown code, then
 * hides only a trailing unfinished dollar-delimited formula during SSE streaming.
 */
export function normalizeMathMarkdown(source: string): string {
  const normalized = splitCodeSegments(source)
    .map((segment) => segment.code
      ? segment.value
      : replaceLegacyDelimiters(removeInvalidComparisonEscapes(segment.value)))
    .join('')
  return guardUnfinishedDollarMath(normalizeDisplaySpacing(normalized))
}
