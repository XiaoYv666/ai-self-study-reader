// 极简 toast：模块级订阅 + <Toaster/> 渲染（视觉照原型 #toast）
import { useEffect, useState } from 'react'

type Listener = (msg: string | null) => void
const listeners = new Set<Listener>()
let timer: ReturnType<typeof setTimeout> | null = null

export function toast(msg: string, durationMs = 2200) {
  listeners.forEach((fn) => fn(msg))
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => listeners.forEach((fn) => fn(null)), durationMs)
}

export function Toaster() {
  const [msg, setMsg] = useState<string | null>(null)
  useEffect(() => {
    const fn: Listener = (m) => setMsg(m)
    listeners.add(fn)
    return () => {
      listeners.delete(fn)
    }
  }, [])
  return (
    <div id="toast" className={msg ? 'show' : ''}>
      <span className="ok">✓</span>
      <span>{msg}</span>
    </div>
  )
}
