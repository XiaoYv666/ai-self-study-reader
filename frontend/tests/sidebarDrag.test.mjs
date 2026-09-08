import assert from 'node:assert/strict'
import test from 'node:test'
import { applySidebarDrop, containerOfIn, moveItemAcrossContainers, validateUniqueChildItems } from '../.test-dist/sidebarDrag.js'

const base = () => ({
  subjects: ['sub:1'],
  'children:1:root': ['folder:1', 'doc:5', 'folder:3', 'folder:4'],
  'children:1:1': ['folder:2'],
  'children:1:2': ['doc:2', 'doc:3', 'doc:4'],
  'children:1:3': [],
  'children:1:4': [],
})

function occurrences(items, itemId) {
  return Object.entries(items)
    .filter(([key]) => key.startsWith('children:'))
    .flatMap(([, list]) => list)
    .filter((id) => id === itemId).length
}

test('moves a document from nested folder to root exactly once', () => {
  const moved = moveItemAcrossContainers(base(), 'doc:3', 'children:1:root', 2)
  assert.deepEqual(moved['children:1:2'], ['doc:2', 'doc:4'])
  assert.deepEqual(moved['children:1:root'], ['folder:1', 'doc:5', 'doc:3', 'folder:3', 'folder:4'])
  assert.equal(containerOfIn(moved, 'doc:3'), 'children:1:root')
  assert.equal(occurrences(moved, 'doc:3'), 1)
  assert.doesNotThrow(() => validateUniqueChildItems(moved))
})

test('removes stale duplicates globally while moving through several containers', () => {
  const stale = base()
  stale['children:1:root'] = [...stale['children:1:root'], 'doc:3']
  let moved = moveItemAcrossContainers(stale, 'doc:3', 'children:1:3', 0)
  moved = moveItemAcrossContainers(moved, 'doc:3', 'children:1:4', 0)
  moved = moveItemAcrossContainers(moved, 'doc:3', 'children:1:root', 1)
  assert.equal(occurrences(moved, 'doc:3'), 1)
  assert.deepEqual(moved['children:1:2'], ['doc:2', 'doc:4'])
  assert.deepEqual(moved['children:1:3'], [])
  assert.deepEqual(moved['children:1:4'], [])
  assert.deepEqual(moved['children:1:root'], ['folder:1', 'doc:3', 'doc:5', 'folder:3', 'folder:4'])
})

test('reorders mixed siblings without losing folders or documents', () => {
  const moved = moveItemAcrossContainers(base(), 'doc:5', 'children:1:root', 3)
  assert.deepEqual(moved['children:1:root'], ['folder:1', 'folder:3', 'folder:4', 'doc:5'])
  assert.equal(occurrences(moved, 'doc:5'), 1)
})

test('rejects duplicate child membership before building requests', () => {
  const invalid = base()
  invalid['children:1:3'] = ['doc:2']
  assert.throws(() => validateUniqueChildItems(invalid), /doc:2.*多个容器/)
})

test('drops a nested document between root folders as a sibling', () => {
  const moved = applySidebarDrop(base(), 'doc:3', 'insert:children:1:root:2')
  assert.deepEqual(moved['children:1:root'], ['folder:1', 'doc:5', 'doc:3', 'folder:3', 'folder:4'])
  assert.deepEqual(moved['children:1:2'], ['doc:2', 'doc:4'])
  assert.equal(occurrences(moved, 'doc:3'), 1)
})

test('drops documents at root first and last insertion zones', () => {
  const first = applySidebarDrop(base(), 'doc:3', 'insert:children:1:root:0')
  assert.equal(first['children:1:root'][0], 'doc:3')
  assert.equal(occurrences(first, 'doc:3'), 1)

  const last = applySidebarDrop(base(), 'doc:3', 'insert:children:1:root:4')
  assert.deepEqual(last['children:1:root'], ['folder:1', 'doc:5', 'folder:3', 'folder:4', 'doc:3'])
  assert.equal(occurrences(last, 'doc:3'), 1)
})

test('enters a folder only through its dedicated center drop target', () => {
  const moved = applySidebarDrop(base(), 'doc:5', 'enter-folder:3')
  assert.deepEqual(moved['children:1:root'], ['folder:1', 'folder:3', 'folder:4'])
  assert.deepEqual(moved['children:1:3'], ['doc:5'])
  assert.equal(occurrences(moved, 'doc:5'), 1)
})

test('reorders a folder only within same-subject root containers', () => {
  const reordered = applySidebarDrop(base(), 'folder:4', 'insert:children:1:root:1')
  assert.deepEqual(reordered['children:1:root'], ['folder:1', 'folder:4', 'doc:5', 'folder:3'])
})

test('root folder enters another root-level folder (cross-level move, depth ok)', () => {
  const moved = applySidebarDrop(base(), 'folder:4', 'enter-folder:3')
  assert.deepEqual(moved['children:1:root'], ['folder:1', 'doc:5', 'folder:3'])
  assert.deepEqual(moved['children:1:3'], ['folder:4'])
  assert.equal(occurrences(moved, 'folder:4'), 1)
  assert.doesNotThrow(() => validateUniqueChildItems(moved))
})

test('nested (depth-2) folder can move to another root-level folder (still depth-2)', () => {
  const moved = applySidebarDrop(base(), 'folder:2', 'enter-folder:3')
  assert.deepEqual(moved['children:1:1'], [])
  assert.deepEqual(moved['children:1:3'], ['folder:2'])
  assert.equal(containerOfIn(moved, 'folder:2'), 'children:1:3')
  assert.doesNotThrow(() => validateUniqueChildItems(moved))
})

test('folder containing a subfolder cannot enter another folder (would exceed depth 2)', () => {
  // folder:1 contains folder:2 → subtree height 2 → entering any folder is rejected
  const unchanged = applySidebarDrop(base(), 'folder:1', 'enter-folder:3')
  assert.deepEqual(unchanged, base())
})

test('folder cannot be inserted into a nested container; cross-subject root insert allowed', () => {
  // 嵌套容器（folder:1 内部）拒绝根级文件夹插入
  const nested = applySidebarDrop(base(), 'folder:4', 'insert:children:1:1:1')
  assert.deepEqual(nested, base())

  // 跨科目容器：允许插到目标科目根级（确认弹窗在组件层拦截）
  const foreign = { ...base(), 'children:9:root': ['folder:7'] }
  const moved = applySidebarDrop(foreign, 'folder:7', 'insert:children:1:root:1')
  assert.deepEqual(moved['children:9:root'], [])
  assert.deepEqual(moved['children:1:root'], ['folder:1', 'folder:7', 'doc:5', 'folder:3', 'folder:4'])
  assert.equal(containerOfIn(moved, 'folder:7'), 'children:1:root')
  assert.doesNotThrow(() => validateUniqueChildItems(moved))
})

test('document still enters folder center and folder cannot enter itself', () => {
  const moved = applySidebarDrop(base(), 'doc:5', 'enter-folder:3')
  assert.deepEqual(moved['children:1:3'], ['doc:5'])

  const self = applySidebarDrop(base(), 'folder:3', 'enter-folder:3')
  assert.deepEqual(self, base())
})

test('folder cannot be dropped into a folder inside its own subtree (cycle)', () => {
  // folder:1 含子文件夹 folder:2；把 folder:1 拖进 folder:2 = 环，必须拒绝
  const cycled = applySidebarDrop(base(), 'folder:1', 'enter-folder:2')
  assert.deepEqual(cycled, base())
})

test('depth-2 folder can return to subject root via insert line', () => {
  const moved = applySidebarDrop(base(), 'folder:2', 'insert:children:1:root:3')
  assert.deepEqual(moved['children:1:1'], [])
  assert.deepEqual(moved['children:1:root'], ['folder:1', 'doc:5', 'folder:3', 'folder:2', 'folder:4'])
  assert.equal(containerOfIn(moved, 'folder:2'), 'children:1:root')
  assert.doesNotThrow(() => validateUniqueChildItems(moved))
})
