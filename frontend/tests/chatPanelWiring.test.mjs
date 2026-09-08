import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const chatPanelUrl = new URL('../src/components/ChatPanel.tsx', import.meta.url)

async function source() {
  return readFile(chatPanelUrl, 'utf8')
}

test('ChatPanel passes the active AbortSignal into chatStream', async () => {
  const panel = await source()
  assert.match(panel, /chatStream\([\s\S]*?signal:\s*run\.controller\.signal/)
})

test('streaming swaps the send action for an enabled stop button', async () => {
  const panel = await source()
  assert.ok(panel.includes("{streaming ? '停止' : '发送'}"))
  assert.match(panel, /onClick=\{streaming\s*\?\s*stopGenerating/)
  assert.doesNotMatch(panel, /disabled=\{streaming\s*\|\|\s*!input\.trim\(\)\}/)
})

test('new chat aborts before clearing messages and selected pages', async () => {
  const panel = await source()
  const newChat = panel.match(/const newChat = \(\) => \{([\s\S]*?)\n  \}/)?.[1] ?? ''
  assert.ok(newChat.indexOf('stopGenerating()') >= 0)
  assert.ok(newChat.indexOf('stopGenerating()') < newChat.indexOf('setMessages([])'))
  assert.ok(newChat.indexOf('setMessages([])') < newChat.indexOf('onClearPages()'))
})

test('ChatPanel cleanup aborts active requests on unmount and document change', async () => {
  const panel = await source()
  assert.match(panel, /useEffect\(\(\)\s*=>\s*\{[\s\S]*?return \(\) => \{[\s\S]*?runManager\.current\.stop\(\)[\s\S]*?\}\s*\}, \[doc\?\.id\]\)/)
})

test('textarea wiring sends only Shift+Enter outside composition and documents shortcuts', async () => {
  const panel = await source()
  assert.match(panel, /shouldSendFromComposerKey\(\{[\s\S]*?isComposing:\s*e\.nativeEvent\.isComposing/)
  assert.match(panel, /Enter 换行，Shift\+Enter 发送/)
})
