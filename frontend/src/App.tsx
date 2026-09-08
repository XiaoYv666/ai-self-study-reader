// 应用骨架：三栏布局（252px | 1fr | 可拖拽聊天栏 300–540px）+ 全局状态 + 数据加载（live 后端 / mock 降级）
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent } from 'react'
import ChatPanel from './components/ChatPanel'
import ModelSettingsModal from './components/ModelSettingsModal'
import PdfViewer, { type PdfViewerHandle } from './components/PdfViewer'
import SubjectSidebar, { type SidebarDeleteRequest, type SidebarMutation } from './components/SubjectSidebar'
import { createFolder, createSubject, deleteDocument, deleteFolder, deleteSubject, detectBackend, fetchSubjects, fetchTree, forceMock, getMode, onModeChange, pdfSrcFor, uploadFile, DeleteConflictError, type ApiMode } from './lib/api'
import { toast, Toaster } from './lib/toast'
import type { DocumentNode, Subject, TreeResponse } from './types'

export default function App() {
  const [booted, setBooted] = useState(false)
  const [mode, setMode] = useState<ApiMode>('live')
  const [subjects, setSubjects] = useState<Subject[]>([])
  const [trees, setTrees] = useState<Record<string, TreeResponse>>({})
  const [activeSubjectId, setActiveSubjectId] = useState<string | null>(null)
  const [activeDocId, setActiveDocId] = useState<string | null>(null)
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const [selectedPages, setSelectedPages] = useState<Set<number>>(new Set())
  const [modelSettingsOpen, setModelSettingsOpen] = useState(false)
  const [modelConfigRefreshKey, setModelConfigRefreshKey] = useState(0)
  const viewerRef = useRef<PdfViewerHandle>(null)

  // —— 聊天栏宽度拖拽（min 300 / max 540，localStorage 持久化）——
  // 拖动时直接改 #app 上的 --chat-w（CSS 变量），不经过 React state，
  // 因此 mousemove 期间不会触发重渲染，react-pdf 画布完全不受影响。
  const CHAT_MIN = 300
  const CHAT_MAX = 540
  const CHAT_DEFAULT = 372
  const CHAT_KEY = 'chat-panel-width'
  const clampChatWidth = (w: number) => Math.min(CHAT_MAX, Math.max(CHAT_MIN, Math.round(w)))

  const appRef = useRef<HTMLDivElement>(null)
  const resizerRef = useRef<HTMLDivElement>(null)
  const chatWRef = useRef(CHAT_DEFAULT)
  const dragRafRef = useRef<number | null>(null)
  const dragTimerRef = useRef<number | null>(null)
  const lastXRef = useRef(0)

  const applyChatWidth = useCallback((w: number) => {
    chatWRef.current = w
    appRef.current?.style.setProperty('--chat-w', `${w}px`)
    resizerRef.current?.setAttribute('aria-valuenow', String(w))
  }, [])

  // 挂载时从 localStorage 恢复宽度（钳制在 min/max 内）。
  // 注意 #app 要等 booted 后才渲染，因此 effect 依赖 booted，
  // 否则 appRef.current 为 null，恢复会静默失效。
  useEffect(() => {
    if (!booted || !appRef.current) return
    let w = CHAT_DEFAULT
    try {
      const saved = Number(localStorage.getItem(CHAT_KEY))
      if (Number.isFinite(saved) && saved > 0) w = saved
    } catch {
      /* localStorage 不可用时回退默认宽度 */
    }
    applyChatWidth(clampChatWidth(w))
  }, [booted, applyChatWidth])

  const persistChatWidth = useCallback(() => {
    try {
      localStorage.setItem(CHAT_KEY, String(chatWRef.current))
    } catch {
      /* ignore */
    }
  }, [])

  // 实际写宽度的动作：取最近一次光标 x 计算并应用
  const flushResize = useCallback(() => {
    dragRafRef.current = null
    if (dragTimerRef.current != null) {
      clearTimeout(dragTimerRef.current)
      dragTimerRef.current = null
    }
    // 把手骑在聊天栏左缘：宽度 = 视口宽 - 光标 x
    applyChatWidth(clampChatWidth(window.innerWidth - lastXRef.current))
  }, [applyChatWidth])

  // mousemove 节流：优先 rAF（每帧最多一次）；极少数环境（headless / 后台标签）
  // rAF 不触发，50ms 超时兜底保证拖动仍实时生效
  const onResizeMove = useCallback(
    (e: MouseEvent) => {
      lastXRef.current = e.clientX
      if (dragRafRef.current != null) return
      dragRafRef.current = requestAnimationFrame(flushResize)
      dragTimerRef.current = window.setTimeout(() => {
        if (dragRafRef.current != null) flushResize()
      }, 50)
    },
    [flushResize],
  )

  const onResizeUp = useCallback(() => {
    if (dragRafRef.current != null) {
      cancelAnimationFrame(dragRafRef.current)
      dragRafRef.current = null
    }
    if (dragTimerRef.current != null) {
      clearTimeout(dragTimerRef.current)
      dragTimerRef.current = null
    }
    // 补上最后一帧，避免松手瞬间宽度落后
    applyChatWidth(clampChatWidth(window.innerWidth - lastXRef.current))
    window.removeEventListener('mousemove', onResizeMove)
    window.removeEventListener('mouseup', onResizeUp)
    document.body.style.userSelect = ''
    document.body.style.cursor = ''
    resizerRef.current?.classList.remove('dragging')
    persistChatWidth()
  }, [onResizeMove, applyChatWidth, persistChatWidth])

  const startResize = useCallback(
    (e: ReactMouseEvent<HTMLDivElement>) => {
      e.preventDefault()
      lastXRef.current = e.clientX
      document.body.style.userSelect = 'none'
      document.body.style.cursor = 'col-resize'
      resizerRef.current?.classList.add('dragging')
      window.addEventListener('mousemove', onResizeMove)
      window.addEventListener('mouseup', onResizeUp)
    },
    [onResizeMove, onResizeUp],
  )

  // 键盘微调（无障碍加分项）：左右方向键 ±16px
  const onResizeKeyDown = useCallback(
    (e: ReactKeyboardEvent<HTMLDivElement>) => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
      e.preventDefault()
      const step = e.key === 'ArrowLeft' ? -16 : 16
      applyChatWidth(clampChatWidth(chatWRef.current + step))
      persistChatWidth()
    },
    [applyChatWidth, persistChatWidth],
  )

  const loadTree = useCallback(async (subjectId: string) => {
    try {
      const t = await fetchTree(subjectId)
      setTrees((prev) => ({ ...prev, [subjectId]: t }))
    } catch (e) {
      toast(`加载课件树失败：${e instanceof Error ? e.message : e}`)
    }
  }, [])

  const boot = useCallback(async () => {
    let m = await detectBackend()
    setMode(m)
    try {
      const subs = await fetchSubjects()
      setSubjects(subs)
      if (subs.length) {
        setActiveSubjectId(subs[0].id)
        await loadTree(subs[0].id)
      }
    } catch {
      // live 请求意外失败 → 强制降级 mock 重试
      forceMock()
      m = getMode()
      setMode(m)
      const subs = await fetchSubjects()
      setSubjects(subs)
      if (subs.length) {
        setActiveSubjectId(subs[0].id)
        await loadTree(subs[0].id)
      }
    }
    setBooted(true)
  }, [loadTree])

  useEffect(() => {
    void boot()
    return onModeChange((mm) => setMode(mm))
  }, [boot])

  const activeDoc: DocumentNode | null = useMemo(() => {
    if (!activeDocId) return null
    for (const t of Object.values(trees)) {
      const d = t.documents.find((x) => x.id === activeDocId)
      if (d) return d
    }
    return null
  }, [activeDocId, trees])

  const selectSubject = useCallback(
    async (id: string) => {
      setActiveSubjectId(id)
      setCollapsed((c) => ({ ...c, [id]: false }))
      if (!trees[id]) await loadTree(id)
    },
    [trees, loadTree],
  )

  const toggleSubject = useCallback((id: string) => {
    setCollapsed((c) => ({ ...c, [id]: !(c[id] ?? false) }))
  }, [])

  const selectDoc = useCallback((id: string) => {
    setActiveDocId(id)
    setSelectedPages(new Set())
  }, [])

  const togglePage = useCallback((p: number) => {
    setSelectedPages((prev) => {
      const next = new Set(prev)
      if (next.has(p)) next.delete(p)
      else next.add(p)
      return next
    })
  }, [])

  const clearSelection = useCallback(() => setSelectedPages(new Set()), [])

  const handleCreateSubject = useCallback(
    async (name: string) => {
      try {
        const { subject_id } = await createSubject(name)
        const subs = await fetchSubjects()
        setSubjects(subs)
        setActiveSubjectId(subject_id)
        setCollapsed((c) => ({ ...c, [subject_id]: false }))
        await loadTree(subject_id)
        toast(`已创建科目「${name}」`)
      } catch (e) {
        toast(`创建失败：${e instanceof Error ? e.message : e}`)
      }
    },
    [loadTree],
  )

  const handleCreateFolder = useCallback(
    async (subjectId: string, name: string) => {
      try {
        await createFolder(subjectId, name)
        await loadTree(subjectId)
        toast(`已创建文件夹「${name}」`)
      } catch (e) {
        toast(`创建失败：${e instanceof Error ? e.message : e}`)
      }
    },
    [loadTree],
  )

  const handleUpload = useCallback(
    async (file: File) => {
      if (!activeSubjectId) {
        toast('请先选择科目')
        return
      }
      toast(`正在上传「${file.name}」…`)
      try {
        const res = await uploadFile(activeSubjectId, null, file)
        await loadTree(activeSubjectId)
        setActiveDocId(res.doc_id)
        setSelectedPages(new Set())
        const parts = [`上传成功：${res.page_count} 页`]
        if (res.vision?.total) parts.push(`图片页已进入后台 AI 识别 ${res.vision.done}/${res.vision.total}`)
        else if (res.vision_pages?.length) parts.push(`${res.vision_pages.length} 页为 AI 识别`)
        toast(parts.join('，'), res.vision?.total ? 4200 : undefined)
      } catch (e) {
        toast(`上传失败：${e instanceof Error ? e.message : e}`)
      }
    },
    [activeSubjectId, loadTree],
  )

  /**
   * M3b 侧栏拖拽整理：乐观应用 subjects/trees → 并行 PATCH 落库 →
   * 成功回读刷新（doc_count / 服务端顺序）；失败整体回滚 + toast。
   */
  const handleSidebarMutate = useCallback(
    async (m: SidebarMutation) => {
      const prevSubjects = subjects
      const prevTrees = trees
      if (m.subjects) setSubjects(m.subjects)
      if (m.trees) setTrees((prev) => ({ ...prev, ...m.trees! }))
      try {
        // 拖拽跨容器时须先更新归属，再原子重写源/目标容器顺序。
        for (const op of m.ops) await op()
        if (m.refetchSubjects) {
          const subs = await fetchSubjects()
          setSubjects(subs)
        }
        if (m.refetchTrees?.length) {
          await Promise.all(m.refetchTrees.map((sid) => loadTree(sid)))
        } else if (m.trees) {
          await Promise.all(Object.keys(m.trees).map((sid) => loadTree(sid)))
        }
        if (m.okMsg) toast(m.okMsg)
      } catch (e) {
        setSubjects(prevSubjects)
        setTrees(prevTrees)
        const raw = e instanceof Error ? e.message : String(e)
        const concise = raw.length > 220 ? `${raw.slice(0, 220)}…` : raw
        toast(`保存失败，已还原：${concise}`)
      }
    },
    [subjects, trees, loadTree],
  )

  /**
   * M3c 侧栏删除：行内确认后调用。
   * - 课件：级联清理（后端删 document_pages / conversations / 文件）；删的是当前打开的课件 → 清空阅读区
   * - 科目/文件夹：后端只删空节点；409 → toast 后端统计引导先清空
   * 返回 true = 删除成功（侧栏收起确认行），false = 失败（保持确认行）
   */
  const handleSidebarDelete = useCallback(
    async (req: SidebarDeleteRequest): Promise<boolean> => {
      try {
        if (req.kind === 'doc') {
          await deleteDocument(req.id)
          // 乐观从树中移除该行；重读科目树与科目列表（doc_count）
          setTrees((prev) => {
            const next: Record<string, TreeResponse> = {}
            for (const [sid, t] of Object.entries(prev)) {
              next[sid] = { folders: t.folders, documents: t.documents.filter((d) => d.id !== req.id) }
            }
            return next
          })
          if (activeDocId === req.id) {
            setActiveDocId(null)
            setSelectedPages(new Set())
          }
          if (req.subjectId) await loadTree(req.subjectId)
          const subs = await fetchSubjects()
          setSubjects(subs)
          toast('已删除')
          return true
        }
        if (req.kind === 'folder') {
          await deleteFolder(req.id)
          if (req.subjectId) await loadTree(req.subjectId)
          toast('已删除')
          return true
        }
        // subject
        await deleteSubject(req.id)
        const remaining = subjects.filter((s) => s.id !== req.id)
        setSubjects(remaining)
        setTrees((prev) => {
          const next = { ...prev }
          delete next[req.id]
          return next
        })
        if (activeSubjectId === req.id) {
          const fallback = remaining[0]
          setActiveSubjectId(fallback ? fallback.id : null)
          if (fallback && !trees[fallback.id]) await loadTree(fallback.id)
          if (!fallback) setActiveDocId(null)
        }
        toast('已删除')
        return true
      } catch (e) {
        if (e instanceof DeleteConflictError) {
          // 409：非空不可删 → 引导文案（「先清空：内含 3 个文件夹、2 个课件」）
          toast(e.guide)
        } else {
          toast(`删除失败：${e instanceof Error ? e.message : e}`)
        }
        return false
      }
    },
    [subjects, trees, activeDocId, activeSubjectId, loadTree],
  )

  if (!booted) {
    return (
      <div style={{ height: '100vh', display: 'grid', placeItems: 'center', background: 'var(--bg)' }}>
        <div style={{ textAlign: 'center' }}>
          <div className="logo" style={{ width: 44, height: 44, margin: '0 auto 14px' }}>
            <img src="/ai-logo.svg" alt="" />
          </div>
          <div className="brand" style={{ fontSize: 17 }}>AI 自学阅读器</div>
          <div style={{ marginTop: 10, fontSize: 12, color: 'var(--ink-3)' }}>正在连接…</div>
        </div>
      </div>
    )
  }

  return (
    <>
      <div id="app" ref={appRef}>
        <SubjectSidebar
          subjects={subjects}
          trees={trees}
          activeSubjectId={activeSubjectId}
          activeDocId={activeDocId}
          collapsed={collapsed}
          mode={mode}
          onSelectSubject={(id) => void selectSubject(id)}
          onToggleSubject={toggleSubject}
          onSelectDoc={selectDoc}
          onCreateSubject={handleCreateSubject}
          onCreateFolder={handleCreateFolder}
          onUpload={(f) => void handleUpload(f)}
          onMutate={handleSidebarMutate}
          onDelete={handleSidebarDelete}
          onOpenModelSettings={() => setModelSettingsOpen(true)}
        />
        <PdfViewer
          ref={viewerRef}
          doc={activeDoc}
          pdfSrc={pdfSrcFor(activeDoc)}
          selectedPages={selectedPages}
          onTogglePage={togglePage}
          onClearSelection={clearSelection}
        />
        <ChatPanel
          doc={activeDoc}
          selectedPages={selectedPages}
          onRemovePage={(p) =>
            setSelectedPages((prev) => {
              const next = new Set(prev)
              next.delete(p)
              return next
            })
          }
          onClearPages={clearSelection}
          onPageChipClick={(p) => viewerRef.current?.scrollToPage(p)}
          modelRefreshKey={modelConfigRefreshKey}
        />
        {/* 聊天栏宽度拖拽把手（绝对定位骑在分界线上，右缘 = --chat-w） */}
        <div
          id="chat-resizer"
          ref={resizerRef}
          role="separator"
          aria-orientation="vertical"
          aria-label="调整 AI 助教栏宽度"
          aria-valuemin={CHAT_MIN}
          aria-valuemax={CHAT_MAX}
          aria-valuenow={chatWRef.current}
          tabIndex={0}
          onMouseDown={startResize}
          onKeyDown={onResizeKeyDown}
        />
      </div>
      <ModelSettingsModal open={modelSettingsOpen} onClose={() => {
        setModelSettingsOpen(false)
        setModelConfigRefreshKey((key) => key + 1)
      }} />
      <Toaster />
    </>
  )
}
