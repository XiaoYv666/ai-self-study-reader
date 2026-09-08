import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const chatPanelUrl = new URL('../src/components/ChatPanel.tsx', import.meta.url)
const apiUrl = new URL('../src/lib/api.ts', import.meta.url)
const historyUrl = new URL('../src/components/ConversationHistoryModal.tsx', import.meta.url)

test('generated image metadata is included when saving a conversation', async () => {
  const panel = await readFile(chatPanelUrl, 'utf8')
  const api = await readFile(apiUrl, 'utf8')

  assert.match(panel, /images:\s*m\.images/, 'save payload must retain the live message images array')
  assert.match(
    panel,
    /m\.content\.trim\(\)\s*\|\|\s*\(m\.images\?\.length\s*\?\?\s*0\)\s*>\s*0/,
    'an assistant message containing only an image must still be saveable',
  )
  assert.match(api, /messages:\s*ChatMessage\[\]/, 'save API contract must accept persisted image metadata')
})

test('history conversation renders persisted generated images', async () => {
  const history = await readFile(historyUrl, 'utf8')

  assert.match(
    history,
    /m\.content\.trim\(\)\s*\|\|\s*\(m\.images\?\.length\s*\?\?\s*0\)\s*>\s*0/,
    'history must retain image-only messages',
  )
  assert.match(history, /m\.images\.map/, 'history must iterate persisted images')
  assert.match(history, /<img\s+src=\{img\.url\}/, 'history must render persisted image URLs')
  assert.match(history, /gen-img-fallback/, 'history must retain failed-image cards too')
})
