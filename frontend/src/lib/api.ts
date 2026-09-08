// 后端接口封装（技术文档 §5 约定）
// 后端未启动 / 请求失败时自动降级为内置 mock 数据，保证前端永不白屏。
import type { ChatDelta, ChatMessage, ConversationRecord, DocumentNode, FolderNode, ModelCapability, ModelConfig, ModelProvider, ModelTestResult, Subject, TreeResponse, UploadResult, VisionStatus } from '../types'
import { FALLBACK_MOCK_PDF, MOCK_PDF_BY_DOC, MOCK_SUBJECTS, MOCK_TREES } from './mockData'
import { streamJsonSse } from './abortableSse'

export type ApiMode = 'live' | 'mock'

let mode: ApiMode = 'live'
const modeListeners = new Set<(m: ApiMode) => void>()

export function getMode(): ApiMode {
  return mode
}
export function forceMock() {
  setMode('mock')
}
export function onModeChange(fn: (m: ApiMode) => void): () => void {
  modeListeners.add(fn)
  return () => modeListeners.delete(fn)
}
function setMode(m: ApiMode) {
  if (m === mode) return
  mode = m
  modeListeners.forEach((fn) => fn(m))
}

/** 启动探测：后端在则走真实接口，否则降级 mock（1.2s 超时，不阻塞启动） */
export async function detectBackend(): Promise<ApiMode> {
  try {
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), 1200)
    const res = await fetch('/api/subjects', { signal: ctrl.signal })
    clearTimeout(timer)
    if (res.ok) {
      setMode('live')
    } else {
      setMode('mock')
    }
  } catch {
    setMode('mock')
  }
  return mode
}

/* ───────────── 科目 ───────────── */

export async function fetchSubjects(): Promise<Subject[]> {
  if (mode === 'mock') return [...MOCK_SUBJECTS]
  const res = await fetch('/api/subjects')
  if (!res.ok) throw new Error(`GET /api/subjects ${res.status}`)
  return res.json()
}

export async function createSubject(name: string): Promise<{ subject_id: string }> {
  if (mode === 'mock') {
    const id = `s${Date.now().toString(36)}`
    MOCK_SUBJECTS.push({ id, name, doc_count: 0 })
    MOCK_TREES[id] = { folders: [], documents: [] }
    return { subject_id: id }
  }
  const res = await fetch('/api/subjects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  if (!res.ok) throw new Error(`POST /api/subjects ${res.status}`)
  return res.json()
}

export async function createFolder(subjectId: string, name: string, parentId: string | null = null): Promise<{ folder_id: string }> {
  if (mode === 'mock') {
    const tree = MOCK_TREES[subjectId] ?? { folders: [], documents: [] }
    const folder: FolderNode = { id: `f${Date.now().toString(36)}`, name, parent_id: parentId }
    tree.folders.push(folder)
    MOCK_TREES[subjectId] = tree
    return { folder_id: folder.id }
  }
  const res = await fetch('/api/folders', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ subject_id: subjectId, parent_id: parentId, name }),
  })
  if (!res.ok) throw new Error(`POST /api/folders ${res.status}`)
  return res.json()
}

/* ───────────── 课件树 ───────────── */

export async function fetchTree(subjectId: string): Promise<TreeResponse> {
  if (mode === 'mock') {
    return MOCK_TREES[subjectId] ?? { folders: [], documents: [] }
  }
  const res = await fetch(`/api/tree?subject_id=${encodeURIComponent(subjectId)}`)
  if (!res.ok) throw new Error(`GET /api/tree ${res.status}`)
  return res.json()
}

/* ───────────── 上传 ───────────── */

export async function uploadFile(subjectId: string, folderId: string | null, file: File): Promise<UploadResult> {
  if (mode === 'mock') {
    const id = `d${Date.now().toString(36)}`
    const tree = MOCK_TREES[subjectId] ?? { folders: [], documents: [] }
    tree.documents.push({
      id,
      filename: file.name,
      page_count: 10,
      folder_id: folderId,
    })
    MOCK_TREES[subjectId] = tree
    const s = MOCK_SUBJECTS.find((x) => x.id === subjectId)
    if (s) s.doc_count += 1
    return { doc_id: id, page_count: 10 }
  }
  const fd = new FormData()
  fd.append('subject_id', subjectId)
  if (folderId) fd.append('folder_id', folderId)
  fd.append('file', file)
  const res = await fetch('/api/upload', { method: 'POST', body: fd })
  if (!res.ok) {
    let msg = `POST /api/upload ${res.status}`
    try {
      const j = await res.json()
      if (j.detail) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
    } catch { /* ignore */ }
    throw new Error(msg)
  }
  return res.json()
}

/* ───────────── 排序 / 移动（M3b 拖拽整理） ───────────── */

export interface SubjectPatch {
  name?: string
  sort_order?: number
}

export interface FolderPatch {
  name?: string
  sort_order?: number
}

export interface DocumentPatch {
  subject_id?: string | number
  folder_id?: string | number | null
  sort_order?: number
  name?: string
}

export interface TreeOrderItem {
  type: 'folder' | 'document'
  id: string | number
}

export interface TreeMoveContainer {
  subject_id: string | number
  parent_id: string | number | null
  items: TreeOrderItem[]
}

export interface TreeMoveRequest {
  active: TreeOrderItem
  to_subject_id: string | number
  to_parent_id: string | number | null
  containers: TreeMoveContainer[]
}

async function responseError(prefix: string, res: Response): Promise<Error> {
  const fallback = `${prefix} ${res.status}`
  const text = await res.text().catch(() => '')
  if (!text) return new Error(fallback)
  try {
    const payload = JSON.parse(text) as { detail?: unknown }
    const detail = payload.detail ?? payload
    if (typeof detail === 'string') return new Error(`${fallback}: ${detail}`)
    if (detail && typeof detail === 'object') {
      const record = detail as Record<string, unknown>
      const msg = typeof record.msg === 'string' ? record.msg : JSON.stringify(detail)
      const expected = record.expected_docs ?? record.expected ?? (Array.isArray(record.containers) ? (record.containers[0] as Record<string, unknown>)?.expected_docs : undefined)
      const got = record.got_docs ?? record.got ?? (Array.isArray(record.containers) ? (record.containers[0] as Record<string, unknown>)?.got_docs : undefined)
      const compact = expected !== undefined || got !== undefined ? `；expected=${JSON.stringify(expected)}；got=${JSON.stringify(got)}` : ''
      return new Error(`${fallback}: ${msg}${compact}`)
    }
  } catch { /* response was plain text */ }
  return new Error(`${fallback}: ${text}`)
}

export async function updateTreeOrder(subjectId: string, parentId: string | null, items: TreeOrderItem[]): Promise<{ ok: boolean }> {
  if (mode === 'mock') {
    const tree = MOCK_TREES[subjectId]
    if (tree) items.forEach((item, index) => {
      const node = item.type === 'folder'
        ? tree.folders.find((x) => x.id === String(item.id))
        : tree.documents.find((x) => x.id === String(item.id))
      if (node) node.sort_order = index
    })
    return { ok: true }
  }
  const body = { subject_id: subjectId, parent_id: parentId, items }
  const res = await fetch('/api/tree/order', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const error = await responseError('PATCH /api/tree/order', res)
    if (import.meta.env.DEV) console.error('[tree/order] 保存失败', { body, detail: error.message })
    throw error
  }
  return res.json()
}

export async function updateTreeMove(body: TreeMoveRequest): Promise<{ ok: boolean }> {
  if (mode === 'mock') {
    const activeId = String(body.active.id)
    let activeDoc: DocumentNode | undefined
    if (body.active.type === 'document') {
      for (const tree of Object.values(MOCK_TREES)) {
        const found = tree.documents.find((doc) => doc.id === activeId)
        if (found) {
          activeDoc = found
          tree.documents = tree.documents.filter((doc) => doc.id !== activeId)
          break
        }
      }
      const targetTree = MOCK_TREES[String(body.to_subject_id)] ?? { folders: [], documents: [] }
      if (activeDoc) {
        activeDoc.folder_id = body.to_parent_id == null ? null : String(body.to_parent_id)
        targetTree.documents.push(activeDoc)
        MOCK_TREES[String(body.to_subject_id)] = targetTree
      }
    }
    for (const container of body.containers) {
      const tree = MOCK_TREES[String(container.subject_id)]
      if (!tree) continue
      container.items.forEach((item, index) => {
        const node = item.type === 'folder'
          ? tree.folders.find((folder) => folder.id === String(item.id))
          : tree.documents.find((doc) => doc.id === String(item.id))
        if (node) node.sort_order = index
      })
    }
    return { ok: true }
  }
  const res = await fetch('/api/tree/move', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const error = await responseError('PATCH /api/tree/move', res)
    if (import.meta.env.DEV) console.error('[tree/move] 保存失败', { body, detail: error.message })
    throw error
  }
  return res.json()
}

export async function updateSubject(id: string, patch: SubjectPatch): Promise<{ ok: boolean }> {
  if (mode === 'mock') {
    const s = MOCK_SUBJECTS.find((x) => x.id === id)
    if (s) Object.assign(s, patch)
    return { ok: true }
  }
  const res = await fetch(`/api/subjects/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  if (!res.ok) throw new Error(`PATCH /api/subjects ${res.status}`)
  return res.json()
}

export async function updateFolder(id: string, patch: FolderPatch): Promise<{ ok: boolean }> {
  if (mode === 'mock') {
    for (const tree of Object.values(MOCK_TREES)) {
      const f = tree.folders.find((x) => x.id === id)
      if (f) Object.assign(f, patch)
    }
    return { ok: true }
  }
  const res = await fetch(`/api/folders/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  if (!res.ok) throw new Error(`PATCH /api/folders ${res.status}`)
  return res.json()
}

export async function updateDocument(id: string, patch: DocumentPatch): Promise<{ ok: boolean }> {
  if (mode === 'mock') {
    let sourceSubjectId: string | null = null
    let doc: DocumentNode | null = null
    for (const [subjectId, tree] of Object.entries(MOCK_TREES)) {
      const found = tree.documents.find((x) => x.id === id)
      if (found) {
        sourceSubjectId = subjectId
        doc = found
        break
      }
    }
    if (doc) {
      const targetSubjectId = patch.subject_id != null ? String(patch.subject_id) : sourceSubjectId
      if (targetSubjectId && sourceSubjectId && targetSubjectId !== sourceSubjectId) {
        MOCK_TREES[sourceSubjectId].documents = MOCK_TREES[sourceSubjectId].documents.filter((x) => x.id !== id)
        const target = MOCK_TREES[targetSubjectId] ?? { folders: [], documents: [] }
        target.documents.push(doc)
        MOCK_TREES[targetSubjectId] = target
        const sourceSubject = MOCK_SUBJECTS.find((x) => x.id === sourceSubjectId)
        const targetSubject = MOCK_SUBJECTS.find((x) => x.id === targetSubjectId)
        if (sourceSubject) sourceSubject.doc_count = Math.max(0, sourceSubject.doc_count - 1)
        if (targetSubject) targetSubject.doc_count += 1
      }
      if (patch.folder_id !== undefined) doc.folder_id = patch.folder_id as string | null
      if (patch.sort_order !== undefined) doc.sort_order = patch.sort_order
    }
    return { ok: true }
  }
  const res = await fetch(`/api/documents/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  if (!res.ok) throw new Error(`PATCH /api/documents ${res.status}`)
  return res.json()
}

/* ───────────── 删除（科目 / 文件夹 / 课件） ───────────── */

/**
 * 后端 409（科目/文件夹非空不可删）→ 抛出的结构化错误。
 * guide 为引导文案（「先清空：内含 3 个文件夹、2 个课件」），UI 直接 toast。
 */
export class DeleteConflictError extends Error {
  readonly guide: string
  constructor(guide: string, raw: string) {
    super(raw)
    this.name = 'DeleteConflictError'
    this.guide = guide
  }
}

/** 后端 409 detail（含 folder_count / subfolder_count / doc_count）→ 引导文案；无统计返回 null */
function conflictGuide(detail: unknown): string | null {
  if (!detail || typeof detail !== 'object') return null
  const d = detail as Record<string, unknown>
  const parts: string[] = []
  if (typeof d.folder_count === 'number' && d.folder_count > 0) parts.push(`${d.folder_count} 个文件夹`)
  if (typeof d.subfolder_count === 'number' && d.subfolder_count > 0) parts.push(`${d.subfolder_count} 个子文件夹`)
  if (typeof d.doc_count === 'number' && d.doc_count > 0) parts.push(`${d.doc_count} 个课件`)
  return parts.length ? `先清空：内含 ${parts.join('、')}` : null
}

/** DELETE 响应解析：409 → DeleteConflictError；其他非 2xx → 普通 Error */
async function deleteError(prefix: string, res: Response): Promise<Error> {
  const fallback = `${prefix} ${res.status}`
  const text = await res.text().catch(() => '')
  if (!text) return new Error(fallback)
  try {
    const payload = JSON.parse(text) as { detail?: unknown }
    const detail = payload.detail ?? payload
    const msg = typeof detail === 'string'
      ? detail
      : detail && typeof detail === 'object' && typeof (detail as Record<string, unknown>).msg === 'string'
        ? String((detail as Record<string, unknown>).msg)
        : JSON.stringify(detail)
    if (res.status === 409) {
      const guide = conflictGuide(detail)
      return new DeleteConflictError(guide ?? `删除被拒绝：${msg}`, `${prefix} 409: ${msg}`)
    }
    return new Error(`${fallback}: ${msg}`)
  } catch {
    return new Error(`${fallback}: ${text}`)
  }
}

/** 删除科目（只删空科目；非空后端 409 → DeleteConflictError 引导先清空） */
export async function deleteSubject(id: string): Promise<void> {
  if (mode === 'mock') {
    const i = MOCK_SUBJECTS.findIndex((s) => s.id === id)
    if (i < 0) throw new Error(`DELETE /api/subjects/${id} 404`)
    const tree = MOCK_TREES[id]
    if (tree && (tree.folders.length > 0 || tree.documents.length > 0)) {
      throw new DeleteConflictError(
        `先清空：内含 ${tree.folders.length} 个文件夹、${tree.documents.length} 个课件`,
        `DELETE /api/subjects/${id} 409: 科目非空，先移走内容`,
      )
    }
    MOCK_SUBJECTS.splice(i, 1)
    delete MOCK_TREES[id]
    return
  }
  const res = await fetch(`/api/subjects/${encodeURIComponent(id)}`, { method: 'DELETE' })
  if (!res.ok) throw await deleteError(`DELETE /api/subjects/${id}`, res)
}

/** 删除文件夹（只删空文件夹；有子文件夹/课件时 409 → DeleteConflictError） */
export async function deleteFolder(id: string): Promise<void> {
  if (mode === 'mock') {
    for (const tree of Object.values(MOCK_TREES)) {
      if (!tree.folders.some((x) => x.id === id)) continue
      const nSub = tree.folders.filter((x) => x.parent_id === id).length
      const nDocs = tree.documents.filter((x) => x.folder_id === id).length
      if (nSub > 0 || nDocs > 0) {
        throw new DeleteConflictError(
          `先清空：内含 ${nSub} 个子文件夹、${nDocs} 个课件`,
          `DELETE /api/folders/${id} 409: 文件夹非空，先移走内容`,
        )
      }
      tree.folders = tree.folders.filter((x) => x.id !== id)
      return
    }
    throw new Error(`DELETE /api/folders/${id} 404`)
  }
  const res = await fetch(`/api/folders/${encodeURIComponent(id)}`, { method: 'DELETE' })
  if (!res.ok) throw await deleteError(`DELETE /api/folders/${id}`, res)
}

/** 删除课件：后端级联清理 document_pages、conversations 与本地文件 */
export async function deleteDocument(id: string): Promise<void> {
  if (mode === 'mock') {
    for (const [subjectId, tree] of Object.entries(MOCK_TREES)) {
      const i = tree.documents.findIndex((x) => x.id === id)
      if (i < 0) continue
      tree.documents.splice(i, 1)
      const s = MOCK_SUBJECTS.find((x) => x.id === subjectId)
      if (s) s.doc_count = Math.max(0, s.doc_count - 1)
      return
    }
    throw new Error(`DELETE /api/documents/${id} 404`)
  }
  const res = await fetch(`/api/documents/${encodeURIComponent(id)}`, { method: 'DELETE' })
  if (!res.ok) throw await deleteError(`DELETE /api/documents/${id}`, res)
}

/* ───────────── PDF 流 ───────────── */

/** 课件 PDF 的 src：live 走后端流，mock 走 public/mock 静态文件 */
export function pdfSrcFor(doc: DocumentNode | null): string {
  if (!doc) return ''
  if (mode === 'live') return `/api/docs/${doc.id}`
  return MOCK_PDF_BY_DOC[doc.id] ?? FALLBACK_MOCK_PDF
}

/* ───────────── 页面尺寸元数据 ───────────── */

export interface DocPageSize {
  page_no: number
  width: number
  height: number
  source?: 'text' | 'vision'
  has_text?: boolean
  /** 该页已有 AI 图形描述缓存（chat 问图时按需生成，暂不展示） */
  has_fig?: boolean
}

export interface DocPagesResponse {
  pages: DocPageSize[]
  vision: VisionStatus
}

/**
 * 每页实际宽高（pt，上传时 PyMuPDF 抽取）→ 前端按页真实宽高比渲染。
 * mock 模式无此接口，返回空数组（调用方兜底 16:9）；live 失败时抛错由调用方兜底。
 */
export async function getDocPages(docId: string): Promise<DocPagesResponse> {
  if (mode === 'mock') return { pages: [], vision: { status: 'done', done: 0, total: 0, failed_pages: [] } }
  const res = await fetch(`/api/docs/${encodeURIComponent(docId)}/pages`)
  if (!res.ok) throw new Error(`GET /api/docs/${docId}/pages ${res.status}`)
  const j = (await res.json()) as { pages?: DocPageSize[]; vision?: VisionStatus }
  return { pages: j.pages ?? [], vision: j.vision ?? { status: 'done', done: 0, total: 0, failed_pages: [] } }
}

export async function rereadVisionPages(docId: string, pages: number[]): Promise<{ vision_pages: number[]; empty_pages: number[] }> {
  const res = await fetch(`/api/docs/${encodeURIComponent(docId)}/vision/reread`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pages }),
  })
  if (!res.ok) throw new Error(`POST /api/docs/${docId}/vision/reread ${res.status}`)
  return res.json()
}

/* ───────────── AI 对话（SSE 流式） ───────────── */

export interface ChatRequest {
  doc_id: string
  pages: number[]
  question: string
  history: ChatMessage[]
  model_config_id?: number
}

/**
 * 对话流：返回 async generator，逐段产出 ChatDelta。
 * live：POST /api/chat 解析 SSE（data: {"type":"delta"...}）。
 * mock：内置答案打字机式流出（含双语 + LaTeX 文本）。
 */
export async function* chatStream(req: ChatRequest, options: { signal?: AbortSignal } = {}): AsyncGenerator<ChatDelta> {
  if (mode === 'live') {
    try {
      for await (const evt of streamJsonSse<ChatDelta>('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          doc_id: req.doc_id,
          pages: req.pages,
          question: req.question,
          history: req.history.slice(-20),
          model_config_id: req.model_config_id,
        }),
      }, options.signal)) {
        if (evt.type === 'error') throw new Error(evt.message || 'AI 返回错误')
        yield evt
      }
      return
    } catch (e) {
      if (options.signal?.aborted) return
      yield { type: 'error', message: e instanceof Error ? e.message : '对话请求失败' }
      return
    }
  }

  // ── mock：打字机流出内置讲解 ──
  const sel = req.pages.length ? [...req.pages].sort((a, b) => a - b) : [1]
  const p1 = sel[0]
  const p2 = sel[sel.length - 1] ?? sel[0]
  const answer = [
    `### 📌 基于 P${sel.join('、P')}`,
    `收到你的问题：「${req.question}」。按 **前置回顾 → 新知识 → 解题** 的顺序来讲。`,
    '### 前置回顾',
    `定积分 $\\int_a^b f(x)\\,dx$ 是区间上对「长度」的累加；二重积分 $\\iint_D f(x,y)\\,dA$ 是区域上对「面积」的累加。格林公式做的，就是把「沿边界走一圈」的积分和「覆盖整个区域」的积分用等号连起来。`,
    '### 新知识点 · 格林公式',
    '$$\n\\oint_C P\\,dx + Q\\,dy = \\iint_D \\left( \\frac{\\partial Q}{\\partial x} - \\frac{\\partial P}{\\partial y} \\right) dA\n$$',
    '**为什么是相减：**把 P、Q 想成场的两个分量，积分结果只取决于场的**旋度（rotation）** $\\partial Q/\\partial x - \\partial P/\\partial y$ —— 它度量场在这点「旋转」的强弱。条件是 C 为区域 D 的正向（逆时针）边界，且 P、Q 有一阶连续偏导。',
    '| 中文 | English |',
    '| --- | --- |',
    '| 曲线积分 | line integral |',
    '| 格林公式 | Green\'s theorem |',
    '| 闭曲线（逆时针） | closed curve (counterclockwise) |',
    '| 路径无关 | path independence |',
    `### 解题步骤 · P${p2} 例题`,
    `> 计算 $\\oint_C (x^2-y)\\,dx + (x+y^2)\\,dy$，C 为单位圆`,
    '1. 检查条件：C 是正向（逆时针）闭曲线；P、Q 一阶连续可导 ✓',
    '2. 求偏导：$\\partial Q/\\partial x = 1$，$\\partial P/\\partial y = -1$',
    '3. 代入格林公式：原式 $= \\iint_D [1-(-1)]\\,dA = \\iint_D 2\\,dA$',
    '4. D 是单位圆盘，面积 $= \\pi$，于是 $\\iint_D 2\\,dA = 2\\pi$',
    '$$\n\\oint_C (x^2-y)\\,dx + (x+y^2)\\,dy = 2\\pi\n$$',
    '哪一步没想通，或者想换一页继续问，直接追问即可。',
  ].join('\n\n')

  const chunkSize = 6
  // 可中断等待：abort 立即返回，正常到点返回；两条路径都移除监听器，避免泄漏
  const waitForChunk = (delay: number) => new Promise<void>((resolve) => {
    const onAbort = () => {
      clearTimeout(timer)
      resolve()
    }
    const timer = setTimeout(() => {
      options.signal?.removeEventListener('abort', onAbort)
      resolve()
    }, delay)
    options.signal?.addEventListener('abort', onAbort, { once: true })
  })
  for (let i = 0; i < answer.length; i += chunkSize) {
    if (options.signal?.aborted) return
    yield { type: 'delta', content: answer.slice(i, i + chunkSize) }
    // 打字机节奏：标点/公式块稍慢
    const tail = answer.slice(i, i + chunkSize)
    const delay = tail.includes('$$') ? 90 : tail.includes('。') || tail.includes('？') ? 60 : 34
    await waitForChunk(delay)
  }
  if (!options.signal?.aborted) yield { type: 'done' }
}

/* ───────────── 对话存档（M3a：保存 + 按页回看） ───────────── */

export interface SaveConversationInput {
  doc_id: number
  pages: number[]
  messages: ChatMessage[]
}

// mock 模式的内存存档（保证降级时保存/回看闭环可用）
let mockConvSeq = 0
const MOCK_CONVERSATIONS: ConversationRecord[] = []

/** 保存一次对话 → { conv_id }（live：POST /api/conversations） */
export async function saveConversation(input: SaveConversationInput): Promise<{ conv_id: number }> {
  if (mode === 'mock') {
    const conv: ConversationRecord = {
      id: ++mockConvSeq,
      doc_id: input.doc_id,
      pages: input.pages,
      messages: input.messages,
      created_at: new Date().toLocaleString('sv-SE').replace('T', ' '),
    }
    MOCK_CONVERSATIONS.push(conv)
    return { conv_id: conv.id }
  }
  const res = await fetch('/api/conversations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) throw new Error(`POST /api/conversations ${res.status}`)
  return res.json()
}

/** 某课件（可选按页过滤）的对话列表，按 id 倒序（live：GET /api/conversations） */
export async function getConversations(docId: number, page?: number): Promise<ConversationRecord[]> {
  if (mode === 'mock') {
    return MOCK_CONVERSATIONS.filter(
      (c) => c.doc_id === docId && (page == null || c.pages.includes(page)),
    ).reverse()
  }
  const q = new URLSearchParams({ doc_id: String(docId) })
  if (page != null) q.set('page', String(page))
  const res = await fetch(`/api/conversations?${q.toString()}`)
  if (!res.ok) throw new Error(`GET /api/conversations ${res.status}`)
  return res.json()
}

/** 删除一条存档对话（live：DELETE /api/conversations/{id}；404 等失败抛错） */
export async function deleteConversation(id: number): Promise<void> {
  if (mode === 'mock') {
    const i = MOCK_CONVERSATIONS.findIndex((c) => c.id === id)
    if (i < 0) throw new Error(`DELETE /api/conversations/${id} 404`)
    MOCK_CONVERSATIONS.splice(i, 1)
    return
  }
  const res = await fetch(`/api/conversations/${id}`, { method: 'DELETE' })
  if (!res.ok && res.status !== 204) throw new Error(`DELETE /api/conversations/${id} ${res.status}`)
}

/* ───────────── 模型与 API 设置（P2b） ───────────── */

export interface ModelProviderInput {
  name?: string
  type?: string
  base_url?: string
  api_key?: string
  auth_type?: string
  extra_headers?: Record<string, string>
  enabled?: boolean
  note?: string
}

export interface ModelConfigInput {
  provider_id?: number
  model_name?: string
  capability?: ModelCapability
  enabled?: boolean
  priority?: number
  params?: Record<string, unknown>
  note?: string
}

export async function fetchModelProviders(): Promise<ModelProvider[]> {
  if (mode === 'mock') return []
  const res = await fetch('/api/model-providers')
  if (!res.ok) throw new Error(`GET /api/model-providers ${res.status}`)
  return res.json()
}

export async function createModelProvider(input: ModelProviderInput): Promise<{ provider_id: number }> {
  const res = await fetch('/api/model-providers', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) throw await responseError('POST /api/model-providers', res)
  return res.json()
}

export async function updateModelProvider(id: number, input: ModelProviderInput): Promise<ModelProvider> {
  const res = await fetch(`/api/model-providers/${encodeURIComponent(String(id))}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) throw await responseError('PATCH /api/model-providers', res)
  return res.json()
}

export async function deleteModelProvider(id: number): Promise<void> {
  const res = await fetch(`/api/model-providers/${encodeURIComponent(String(id))}`, { method: 'DELETE' })
  if (!res.ok) throw await responseError('DELETE /api/model-providers', res)
}

export async function fetchModelConfigs(capability?: ModelCapability): Promise<ModelConfig[]> {
  if (mode === 'mock') return []
  const q = capability ? `?capability=${encodeURIComponent(capability)}` : ''
  const res = await fetch(`/api/model-configs${q}`)
  if (!res.ok) throw new Error(`GET /api/model-configs ${res.status}`)
  return res.json()
}

export async function createModelConfig(input: ModelConfigInput): Promise<{ model_config_id: number }> {
  const res = await fetch('/api/model-configs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) throw await responseError('POST /api/model-configs', res)
  return res.json()
}

export async function updateModelConfig(id: number, input: ModelConfigInput): Promise<ModelConfig> {
  const res = await fetch(`/api/model-configs/${encodeURIComponent(String(id))}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) throw await responseError('PATCH /api/model-configs', res)
  return res.json()
}

export async function deleteModelConfig(id: number): Promise<void> {
  const res = await fetch(`/api/model-configs/${encodeURIComponent(String(id))}`, { method: 'DELETE' })
  if (!res.ok) throw await responseError('DELETE /api/model-configs', res)
}

export async function testModelConfig(input: { capability: ModelCapability; provider_id: number; model_name: string; sample?: string }): Promise<ModelTestResult> {
  const res = await fetch('/api/model-configs/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) throw await responseError('POST /api/model-configs/test', res)
  return res.json()
}
