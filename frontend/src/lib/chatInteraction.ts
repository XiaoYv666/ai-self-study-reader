export interface ComposerKey {
  key: string
  shiftKey: boolean
  isComposing: boolean
}

export function shouldSendFromComposerKey(event: ComposerKey): boolean {
  return event.key === 'Enter' && event.shiftKey && !event.isComposing
}

export interface ChatRun {
  id: number
  controller: AbortController
}

export interface ChatRunManager {
  start(): ChatRun
  stop(): boolean
  finish(id: number): boolean
  isCurrent(id: number): boolean
}

export function createChatRunManager(): ChatRunManager {
  let sequence = 0
  let active: ChatRun | null = null

  return {
    start() {
      active?.controller.abort()
      active = { id: ++sequence, controller: new AbortController() }
      return active
    },
    stop() {
      if (!active) return false
      const stopped = active
      active = null
      stopped.controller.abort()
      return true
    },
    finish(id) {
      if (active?.id !== id) return false
      active = null
      return true
    },
    isCurrent(id) {
      return active?.id === id
    },
  }
}
