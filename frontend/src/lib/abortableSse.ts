function isAbortError(error: unknown, signal?: AbortSignal): boolean {
  return signal?.aborted === true || (
    typeof error === 'object'
    && error !== null
    && 'name' in error
    && error.name === 'AbortError'
  )
}

/** Parse JSON `data:` lines from an abortable SSE response. */
export async function* streamJsonSse<T>(
  input: RequestInfo | URL,
  init: RequestInit = {},
  signal?: AbortSignal,
): AsyncGenerator<T> {
  let reader: ReadableStreamDefaultReader<Uint8Array> | null = null
  let aborted = signal?.aborted === true
  const onAbort = () => {
    aborted = true
    void reader?.cancel().catch(() => undefined)
  }
  signal?.addEventListener('abort', onAbort, { once: true })

  try {
    const res = await fetch(input, { ...init, signal })
    if (!res.ok || !res.body) {
      const detail = await res.text().catch(() => '')
      throw new Error(detail ? `SSE request failed: ${res.status}: ${detail}` : `SSE request failed: ${res.status}`)
    }

    reader = res.body.getReader()
    if (aborted) {
      await reader.cancel().catch(() => undefined)
      return
    }

    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (aborted) return
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let idx: number
      while ((idx = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, idx).trim()
        buf = buf.slice(idx + 1)
        if (!line.startsWith('data:')) continue
        const payload = line.slice(5).trim()
        if (!payload) continue
        try {
          yield JSON.parse(payload) as T
        } catch (error) {
          if (error instanceof SyntaxError) continue
          throw error
        }
      }
    }

    const tail = buf.trim()
    if (!aborted && tail.startsWith('data:')) {
      const payload = tail.slice(5).trim()
      if (payload) yield JSON.parse(payload) as T
    }
  } catch (error) {
    if (isAbortError(error, signal)) return
    throw error
  } finally {
    signal?.removeEventListener('abort', onAbort)
    reader?.releaseLock()
  }
}
