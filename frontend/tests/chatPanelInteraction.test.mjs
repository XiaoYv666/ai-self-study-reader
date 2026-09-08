import assert from 'node:assert/strict'
import test from 'node:test'

import { createChatRunManager, shouldSendFromComposerKey } from '../src/lib/chatInteraction.ts'

test('Enter is left to the textarea as a newline and never sends', () => {
  assert.equal(shouldSendFromComposerKey({ key: 'Enter', shiftKey: false, isComposing: false }), false)
})

test('Shift+Enter sends', () => {
  assert.equal(shouldSendFromComposerKey({ key: 'Enter', shiftKey: true, isComposing: false }), true)
})

test('composition prevents Shift+Enter from sending', () => {
  assert.equal(shouldSendFromComposerKey({ key: 'Enter', shiftKey: true, isComposing: true }), false)
})

test('stopping an active run really aborts its AbortController', () => {
  const manager = createChatRunManager()
  const run = manager.start()
  assert.equal(run.controller.signal.aborted, false)

  manager.stop()
  assert.equal(run.controller.signal.aborted, true)
  assert.equal(manager.isCurrent(run.id), false)
})

test('starting a replacement run aborts the old run and old completion cannot clear the new run', () => {
  const manager = createChatRunManager()
  const oldRun = manager.start()
  const newRun = manager.start()

  assert.equal(oldRun.controller.signal.aborted, true)
  assert.equal(newRun.controller.signal.aborted, false)
  assert.equal(manager.finish(oldRun.id), false)
  assert.equal(manager.isCurrent(newRun.id), true)
  assert.equal(manager.finish(newRun.id), true)
})

test('new conversation reset invalidates the old stream before its later events arrive', () => {
  const manager = createChatRunManager()
  const oldRun = manager.start()
  manager.stop()

  assert.equal(oldRun.controller.signal.aborted, true)
  assert.equal(manager.isCurrent(oldRun.id), false)
})
