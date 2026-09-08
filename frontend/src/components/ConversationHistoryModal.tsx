// M3a 按页回看：某课件某页关联的历史对话「浮动面板」（只读，复用 .msg/.bubble 渲染样式）
// 打开时 GET /api/conversations?doc_id=&page= → 列表（时间 + 摘要），点开一条展开完整对话
// 支持删除单条：行内确认（防误删）→ DELETE → 移出列表
// M3b 改造：去掉全屏遮罩 → 无背景 fixed 容器（pointer-events:none），卡片自身可交互、背景（课件翻页/勾选）不受影响；
// 标题栏可拖动、右下角可缩放；位置和尺寸分别持久化，始终 clamp 在视口内
import { useEffect, useMemo, useRef, useState } from 'react'
import { deleteConversation, getConversations } from '../lib/api'
import { Markdown } from '../lib/markdown'
import { toast } from '../lib/toast'
import type { ConversationRecord, DocumentNode } from '../types'

const SOURCE_LABEL: Record<string, string> = {
  plot: '精确绘制',
  waveform: '波形图',
  boolean: '布尔图',
  circuit: '电路图',
  draw: 'AI 生成',
}

interface Props {
  doc: DocumentNode | null
  /** 当前查看的页码；null = 关闭 */
  page: number | null
  onClose: () => void
}

/** 后端 created_at "YYYY-MM-DD HH:MM:SS" → "MM-DD HH:MM" */
const fmtTime = (s: string): string => {
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(s)
  if (m) return `${m[2]}-${m[3]} ${m[4]}:${m[5]}`
  return s
}

/** 摘要：去 markdown 符号 + 压缩空白，截断 56 字 */
const summarize = (t: string): string => {
  const clean = t
    .replace(/[#*_`>[\]()$\\|~]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
  return clean.length > 56 ? `${clean.slice(0, 56)}…` : clean
}

// ─── 浮动面板定位 ───────────────────────────────────────────────
const POS_KEY = 'conv-panel-pos'
const SIZE_KEY = 'conv-panel-size'
const DEFAULT_SIZE = { width: 430, height: 560 }
const MIN_WIDTH = 340
const MIN_HEIGHT = 360

interface PanelPos {
  x: number
  y: number
}

interface PanelSize {
  width: number
  height: number
}

const sizeLimits = () => ({
  maxWidth: Math.max(0, Math.min(760, window.innerWidth - 20)),
  maxHeight: Math.max(0, Math.min(window.innerHeight * 0.85, window.innerHeight - 20)),
})

/** 小视口下优先保证面板留在屏内；正常视口严格遵守 340×360 最小尺寸。 */
const clampSize = (width: number, height: number): PanelSize => {
  const { maxWidth, maxHeight } = sizeLimits()
  return {
    width: Math.min(Math.max(width, Math.min(MIN_WIDTH, maxWidth)), maxWidth),
    height: Math.min(Math.max(height, Math.min(MIN_HEIGHT, maxHeight)), maxHeight),
  }
}

/** 位置与当前尺寸共同 clamp，确保整个面板不超出屏幕。 */
const clampPos = (x: number, y: number, size: PanelSize): PanelPos => ({
  x: Math.min(Math.max(x, 0), Math.max(0, window.innerWidth - size.width)),
  y: Math.min(Math.max(y, 0), Math.max(0, window.innerHeight - size.height)),
})

const loadSize = (): PanelSize => {
  try {
    const raw = localStorage.getItem(SIZE_KEY)
    if (raw) {
      const s = JSON.parse(raw) as Partial<PanelSize> | null
      if (s && typeof s.width === 'number' && typeof s.height === 'number') return clampSize(s.width, s.height)
    }
  } catch {
    /* 损坏数据则回退默认 */
  }
  return clampSize(DEFAULT_SIZE.width, DEFAULT_SIZE.height)
}

/** 打开时读 localStorage；无记录默认在阅读区右上（right ≈ 420px 避开右侧聊天栏，top 80px） */
const loadPos = (size: PanelSize): PanelPos => {
  try {
    const raw = localStorage.getItem(POS_KEY)
    if (raw) {
      const p = JSON.parse(raw) as Partial<PanelPos> | null
      if (p && typeof p.x === 'number' && typeof p.y === 'number') return clampPos(p.x, p.y, size)
    }
  } catch {
    /* 损坏数据则回退默认 */
  }
  return clampPos(window.innerWidth - 420 - size.width, 80, size)
}

export default function ConversationHistoryModal({ doc, page, onClose }: Props) {
  const [convs, setConvs] = useState<ConversationRecord[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<number | null>(null)
  /** 正在等行内确认删除的对话 id */
  const [confirmId, setConfirmId] = useState<number | null>(null)
  /** 正在执行 DELETE 的对话 id（置灰防重复点击） */
  const [deletingId, setDeletingId] = useState<number | null>(null)

  // 面板位置与尺寸（fixed 坐标），各自兼容独立的 localStorage 记录
  const [size, setSize] = useState<PanelSize>(() => loadSize())
  const [pos, setPos] = useState<PanelPos>(() => {
    const initialSize = loadSize()
    return loadPos(initialSize)
  })
  const [dragging, setDragging] = useState(false)
  const [resizing, setResizing] = useState(false)
  /** 按下时鼠标与面板左上角的偏移；mouseup 时用它持久化最新位置 */
  const dragRef = useRef<{ dx: number; dy: number } | null>(null)
  const posRef = useRef(pos)
  posRef.current = pos
  const sizeRef = useRef(size)
  sizeRef.current = size
  const resizeRef = useRef<{ startX: number; startY: number; width: number; height: number } | null>(null)
  const resizeFrameRef = useRef<number | null>(null)
  const pendingResizeRef = useRef<{ clientX: number; clientY: number } | null>(null)

  const docId = doc ? Number(doc.id) : 0

  useEffect(() => {
    if (page == null) return
    let cancelled = false
    setConvs([])
    setExpandedId(null)
    setConfirmId(null)
    setDeletingId(null)
    setLoading(true)
    setError(null)
    getConversations(docId, page)
      .then((list) => {
        if (!cancelled) setConvs(list)
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [docId, page])

  // Esc 关闭
  useEffect(() => {
    if (page == null) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [page, onClose])

  // 拖动：document 级 mousemove/mouseup（拖出标题栏仍有效）；结束写 localStorage
  useEffect(() => {
    if (!dragging) return
    const onMove = (e: MouseEvent) => {
      const d = dragRef.current
      if (!d) return
      setPos(clampPos(e.clientX - d.dx, e.clientY - d.dy, sizeRef.current))
    }
    const onUp = () => {
      dragRef.current = null
      setDragging(false)
      try {
        localStorage.setItem(POS_KEY, JSON.stringify(posRef.current))
      } catch {
        /* 存储不可用则忽略 */
      }
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    // 拖动中全局禁选中，避免划过文字
    const prevSelect = document.body.style.userSelect
    document.body.style.userSelect = 'none'
    return () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      document.body.style.userSelect = prevSelect
    }
  }, [dragging])

  // 缩放：document 级事件 + rAF 节流；结束持久化最终尺寸与联动 clamp 后的位置
  useEffect(() => {
    if (!resizing) return
    const applyResize = () => {
      resizeFrameRef.current = null
      const start = resizeRef.current
      const pointer = pendingResizeRef.current
      if (!start || !pointer) return
      const nextSize = clampSize(
        start.width + pointer.clientX - start.startX,
        start.height + pointer.clientY - start.startY,
      )
      sizeRef.current = nextSize
      setSize(nextSize)
      setPos((current) => {
        const nextPos = clampPos(current.x, current.y, nextSize)
        posRef.current = nextPos
        return nextPos
      })
    }
    const onMove = (e: MouseEvent) => {
      pendingResizeRef.current = { clientX: e.clientX, clientY: e.clientY }
      if (resizeFrameRef.current == null) resizeFrameRef.current = requestAnimationFrame(applyResize)
    }
    const onUp = () => {
      if (resizeFrameRef.current != null) {
        cancelAnimationFrame(resizeFrameRef.current)
        applyResize()
      }
      resizeRef.current = null
      pendingResizeRef.current = null
      setResizing(false)
      try {
        localStorage.setItem(SIZE_KEY, JSON.stringify(sizeRef.current))
        localStorage.setItem(POS_KEY, JSON.stringify(posRef.current))
      } catch {
        /* 存储不可用则忽略 */
      }
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    const prevSelect = document.body.style.userSelect
    document.body.style.userSelect = 'none'
    return () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      if (resizeFrameRef.current != null) cancelAnimationFrame(resizeFrameRef.current)
      resizeFrameRef.current = null
      document.body.style.userSelect = prevSelect
    }
  }, [resizing])

  // 浏览器窗口变小时同步收缩尺寸并移回可视区域
  useEffect(() => {
    const onWindowResize = () => {
      const nextSize = clampSize(sizeRef.current.width, sizeRef.current.height)
      const nextPos = clampPos(posRef.current.x, posRef.current.y, nextSize)
      setSize(nextSize)
      setPos(nextPos)
      try {
        localStorage.setItem(SIZE_KEY, JSON.stringify(nextSize))
        localStorage.setItem(POS_KEY, JSON.stringify(nextPos))
      } catch {
        /* 存储不可用则忽略 */
      }
    }
    window.addEventListener('resize', onWindowResize)
    return () => window.removeEventListener('resize', onWindowResize)
  }, [])

  /** 标题栏按下开始拖动（左键；点在按钮上不拖） */
  const onHeadMouseDown = (e: React.MouseEvent) => {
    if (e.button !== 0 || resizing) return
    if ((e.target as HTMLElement).closest('button')) return
    dragRef.current = { dx: e.clientX - pos.x, dy: e.clientY - pos.y }
    setDragging(true)
    e.preventDefault()
  }

  const onResizeMouseDown = (e: React.MouseEvent) => {
    if (e.button !== 0 || dragging) return
    e.stopPropagation()
    e.preventDefault()
    resizeRef.current = {
      startX: e.clientX,
      startY: e.clientY,
      width: size.width,
      height: size.height,
    }
    pendingResizeRef.current = { clientX: e.clientX, clientY: e.clientY }
    setResizing(true)
  }

  const firstQuestion = useMemo(
    () => (c: ConversationRecord) => {
      const q = c.messages.find((m) => m.role === 'user' && m.content && m.content.trim())
      return q ? summarize(q.content) : '（无提问内容）'
    },
    [],
  )

  /** 确认删除 → DELETE → 成功移出列表 + toast；失败 toast 错误并恢复可交互 */
  const handleDelete = async (id: number) => {
    setDeletingId(id)
    try {
      await deleteConversation(id)
      setConvs((prev) => prev.filter((c) => c.id !== id))
      if (expandedId === id) setExpandedId(null)
      toast('已删除')
    } catch (e) {
      toast(`删除失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setConfirmId(null)
      setDeletingId(null)
    }
  }

  if (page == null) return null

  return (
    // 无遮罩 fixed 容器：pointer-events:none → 背景课件可正常翻页/勾选；仅卡片自身接事件
    <div className="conv-overlay">
      <div
        className={`conv-panel${dragging ? ' dragging' : ''}${resizing ? ' resizing' : ''}`}
        role="dialog"
        aria-label={`P${page} 页历史对话`}
        style={{ left: pos.x, top: pos.y, width: size.width, height: size.height }}
      >
        <div className="conv-head" onMouseDown={onHeadMouseDown}>
          <div className="conv-title">
            历史对话 <span className="pg">P{page}</span>
          </div>
          <span className="conv-sub">{doc ? doc.filename : ''}</span>
          {convs.length > 0 && <span className="conv-count-badge">{convs.length} 条存档</span>}
          <button className="conv-close" title="关闭" aria-label="关闭" onClick={onClose}>
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        <div className="conv-body">
          {loading && <div className="conv-state">正在加载该页历史对话…</div>}
          {!loading && error && (
            <div className="conv-state err">
              加载失败：{error}
              <br />
              请确认后端服务可用后重试
            </div>
          )}
          {!loading && !error && convs.length === 0 && (
            <div className="conv-state">
              该页还没有存档的对话。
              <br />
              在右侧 AI 助教基于本页提问并点「保存本次对话」后，会出现在这里。
            </div>
          )}

          {!loading &&
            !error &&
            convs.map((c) => {
              const open = expandedId === c.id
              const confirming = confirmId === c.id
              const deleting = deletingId === c.id
              const msgs = c.messages.filter((m) => m.content.trim() || (m.images?.length ?? 0) > 0)
              return (
                <div
                  key={c.id}
                  className={`conv-item${open ? ' open' : ''}${deleting ? ' deleting' : ''}`}
                  onClick={() => {
                    if (deleting) return
                    if (confirming) return
                    setConfirmId(null)
                    setExpandedId(open ? null : c.id)
                  }}
                  role="button"
                  tabIndex={deleting ? -1 : 0}
                  aria-disabled={deleting}
                  onKeyDown={(e) => {
                    if (deleting || confirming) return
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      setExpandedId(open ? null : c.id)
                    }
                  }}
                >
                  <div className="conv-item-head">
                    <span className="conv-time">#{c.id} · {fmtTime(c.created_at)}</span>
                    <span className="conv-pages">
                      {c.pages.map((p) => (
                        <span key={p} className="pg">
                          P{p}
                        </span>
                      ))}
                    </span>
                    <span className="conv-count">{msgs.length} 条消息</span>
                    {!confirming && !deleting && (
                      <button
                        className="conv-del"
                        title="删除此对话"
                        aria-label={`删除对话 #${c.id}`}
                        disabled={deletingId != null || confirmId != null}
                        onClick={(e) => {
                          e.stopPropagation()
                          setConfirmId(c.id)
                        }}
                      >
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
                          <path d="M3 6h18" />
                          <path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2" />
                          <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
                          <line x1="10" y1="11" x2="10" y2="17" />
                          <line x1="14" y1="11" x2="14" y2="17" />
                        </svg>
                      </button>
                    )}
                    {confirming && (
                      <span className="conv-confirm" onClick={(e) => e.stopPropagation()}>
                        <span className="conv-confirm-text">删除后不可恢复</span>
                        <button
                          className="conv-confirm-yes"
                          disabled={deleting}
                          onClick={() => handleDelete(c.id)}
                        >
                          {deleting ? '删除中…' : '确认删除'}
                        </button>
                        <button
                          className="conv-confirm-no"
                          disabled={deleting}
                          onClick={() => setConfirmId(null)}
                        >
                          取消
                        </button>
                      </span>
                    )}
                    {!confirming && <span className="conv-arrow">{open ? '▾' : '▸'}</span>}
                  </div>
                  <div className="conv-summary">{firstQuestion(c)}</div>

                  {open && (
                    <div className="conv-full">
                      {msgs.map((m, i) => (
                        <div key={i} className={`msg ${m.role}`}>
                          <div className="bubble">
                            {m.role === 'user' ? <>{m.content}</> : <Markdown text={m.content} />}
                            {m.role === 'assistant' && m.images && m.images.length > 0 && (
                              <div className="gen-images">
                                {m.images.map((img, k) => (
                                  <figure key={k} className="gen-img-card">
                                    {img.url ? (
                                      <img src={img.url} alt={img.prompt} loading="lazy" />
                                    ) : (
                                      <div className="gen-img-fallback">🖼 出图失败：{img.error || '网关异常，稍后可重试'}</div>
                                    )}
                                    <figcaption>
                                      {img.url && img.source && (
                                        <span className="gen-src">
                                          {SOURCE_LABEL[img.source] ?? '图片'}
                                          {img.degraded ? '·降级生成' : ''}
                                        </span>
                                      )}
                                      {img.prompt}
                                    </figcaption>
                                  </figure>
                                ))}
                              </div>
                            )}
                          </div>
                          <div className="meta">{m.role === 'user' ? '我' : 'AI 助教'}</div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )
            })}
        </div>
        <div
          className="conv-resize-handle"
          role="separator"
          aria-label="调整历史对话面板大小"
          onMouseDown={onResizeMouseDown}
        />
      </div>
    </div>
  )
}
