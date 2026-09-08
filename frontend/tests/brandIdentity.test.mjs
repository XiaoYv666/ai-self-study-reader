import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import test from 'node:test'

const sidebarSource = readFileSync(new URL('../src/components/SubjectSidebar.tsx', import.meta.url), 'utf8')
const appSource = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')
const logoUrl = new URL('../public/ai-logo.svg', import.meta.url)

test('brand uses the generic AI logo and open-source name everywhere it is rendered', () => {
  assert.match(sidebarSource, /src="\/ai-logo\.svg"/)
  assert.match(sidebarSource, /AI 自学阅读器/)
  assert.match(appSource, /src="\/ai-logo\.svg"/)
  assert.match(appSource, /AI 自学阅读器/)
  assert.equal(existsSync(logoUrl), true)
  assert.match(styles, /\.logo img\s*\{[^}]*object-fit:\s*cover/s)
})

test('retired paper-version tag and its dedicated style are absent', () => {
  assert.doesNotMatch(sidebarSource, /V2\s*·\s*纸感/)
  assert.doesNotMatch(sidebarSource, /sb-tag/)
  assert.doesNotMatch(styles, /\.sb-tag\s*\{/)
})
