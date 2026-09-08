import assert from 'node:assert/strict'
import test from 'node:test'

import { streamJsonSse } from '../src/lib/abortableSse.ts'

async function collect(stream) {
  const events = []
  for await (const event of stream) events.push(event)
  return events
}

test('passes AbortSignal to fetch', async (t) => {
  const originalFetch = globalThis.fetch
  t.after(() => { globalThis.fetch = originalFetch })

  let receivedSignal
  globalThis.fetch = async (_url, init) => {
    receivedSignal = init?.signal
    return new Response('data: {"type":"done"}\n', { status: 200 })
  }

  const controller = new AbortController()
  await collect(streamJsonSse('/api/chat', { method: 'POST' }, controller.signal))
  assert.equal(receivedSignal, controller.signal)
})

test('active abort ends the SSE stream without converting AbortError into data', async (t) => {
  const originalFetch = globalThis.fetch
  t.after(() => { globalThis.fetch = originalFetch })

  globalThis.fetch = async (_url, init) => new Promise((_resolve, reject) => {
    init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
  })

  const controller = new AbortController()
  const collected = collect(streamJsonSse('/api/chat', {}, controller.signal))
  controller.abort()

  assert.deepEqual(await collected, [])
})

test('aborting after a partial delta preserves it and cancels the response reader', async (t) => {
  const originalFetch = globalThis.fetch
  t.after(() => { globalThis.fetch = originalFetch })

  let cancelled = false
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('data: {"type":"delta","content":"partial"}\n'))
    },
    cancel() { cancelled = true },
  })
  globalThis.fetch = async () => new Response(body, { status: 200 })

  const controller = new AbortController()
  const events = []
  for await (const event of streamJsonSse('/api/chat', {}, controller.signal)) {
    events.push(event)
    controller.abort()
  }

  assert.deepEqual(events, [{ type: 'delta', content: 'partial' }])
  assert.equal(cancelled, true)
})

test('ordinary network failures still reject for the caller to report', async (t) => {
  const originalFetch = globalThis.fetch
  t.after(() => { globalThis.fetch = originalFetch })
  globalThis.fetch = async () => { throw new Error('network down') }

  await assert.rejects(() => collect(streamJsonSse('/api/chat')), /network down/)
})
