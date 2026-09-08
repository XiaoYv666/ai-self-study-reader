import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const componentUrl = new URL('../src/components/ModelSettingsModal.tsx', import.meta.url)
const cssUrl = new URL('../src/index.css', import.meta.url)

test('model settings includes a compact, accessible API setup guide', async () => {
  const source = await readFile(componentUrl, 'utf8')
  const requiredCopy = [
    '模型与 API 配置教程',
    'aria-label="打开模型与 API 配置教程"',
    '程序化函数图、波形图、布尔/电路图',
    'https://api.example.com/v1',
    'sk-...',
    'your-model-name',
    '数字越小越优先',
    '只为当前对话临时切换',
    '不会在一次请求失败后自动尝试下一模型',
    '401 / 403',
    '404 / model_not_found',
    '额外请求头中的敏感值也会掩码',
  ]

  for (const copy of requiredCopy) assert.ok(source.includes(copy), `missing guide copy: ${copy}`)
  assert.ok(source.includes("event.key === 'Escape'"), 'guide should close with Escape')
})

test('model settings guide has restrained responsive paper-style CSS', async () => {
  const css = await readFile(cssUrl, 'utf8')
  for (const selector of ['.model-help-trigger', '.model-guide-mask', '.model-guide', '.model-guide-capabilities']) {
    assert.ok(css.includes(selector), `missing style: ${selector}`)
  }
  assert.ok(css.includes('@media (max-width: 720px)'), 'guide should adapt to narrow windows')
})
