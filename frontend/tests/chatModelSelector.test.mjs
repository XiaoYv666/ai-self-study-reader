import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const chatPanelUrl = new URL('../src/components/ChatPanel.tsx', import.meta.url)
const apiUrl = new URL('../src/lib/api.ts', import.meta.url)
const appUrl = new URL('../src/App.tsx', import.meta.url)

test('chat model selector is driven by enabled page model configuration', async () => {
  const source = await readFile(chatPanelUrl, 'utf8')
  assert.ok(!source.includes('MODEL_OPTIONS'), 'hard-coded fake model options must be removed')
  assert.ok(source.includes("fetchModelConfigs('chat')"), 'chat configs should load from the model API')
  assert.ok(source.includes('fetchModelProviders()'), 'provider enabled state must be loaded')
  assert.ok(source.includes('m.enabled') && source.includes('provider.enabled'), 'disabled models/providers must be filtered')
  assert.ok(source.includes('a.priority - b.priority') && source.includes('a.id - b.id'), 'models must match backend default ordering')
  assert.ok(source.includes('未配置推理模型'), 'empty state should be explicit')
  assert.ok(source.includes('设置 → 模型与 API'), 'send guard should explain where to configure a model')
  assert.ok(source.includes('本次对话使用：'), 'model-switch toast should describe the real session model')
})

test('selected model_config_id is included in the chat request payload', async () => {
  const api = await readFile(apiUrl, 'utf8')
  const panel = await readFile(chatPanelUrl, 'utf8')
  assert.match(api, /model_config_id\?:\s*number/)
  assert.ok(api.includes('model_config_id: req.model_config_id'))
  assert.ok(panel.includes('model_config_id: selectedModelId'))
})

test('closing model settings refreshes the chat selector without a page reload', async () => {
  const app = await readFile(appUrl, 'utf8')
  const panel = await readFile(chatPanelUrl, 'utf8')
  assert.ok(app.includes('modelConfigRefreshKey'))
  assert.ok(app.includes('modelRefreshKey={modelConfigRefreshKey}'))
  assert.ok(panel.includes('modelRefreshKey'))
})