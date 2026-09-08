// 与后端接口约定对齐的数据类型（技术文档 §5）

export interface Subject {
  id: string
  name: string
  doc_count: number
  sort_order?: number
}

export interface FolderNode {
  id: string
  name: string
  parent_id: string | null
  sort_order?: number
}

export interface DocumentNode {
  id: string
  filename: string
  page_count: number
  folder_id: string | null
  sort_order?: number
  /** 上传时后端标记的读图回填页（M2 才有，前端预留标注） */
  vision_pages?: number[]
  vision?: VisionStatus
  failed_pages?: number[]
}

export interface VisionStatus {
  status: 'pending' | 'running' | 'done' | 'partial'
  done: number
  total: number
  failed_pages: number[]
}

export interface TreeResponse {
  folders: FolderNode[]
  documents: DocumentNode[]
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  /** 发送时携带的勾选页（仅本地渲染用，不传给后端 history） */
  pages?: number[]
  /** AI 回答附带的生成图（P2 画图题，正文后渲染图片卡片） */
  images?: ChatImage[]
  /** 该条 AI 回答实际使用的推理模型（仅本地展示） */
  model_label?: string
}

/** SSE image 事件落到消息上的图片卡片（P2） */
export interface ChatImage {
  url: string
  prompt: string
  /** 生成失败时的原因（此时无 url，卡片显示降级文案） */
  error?: string
  /** 图像来源：精确绘制（matplotlib/schemdraw）或 image-model 生图 */
  source?: 'plot' | 'draw' | 'circuit' | 'boolean' | 'waveform'
  /** true = 精确绘制链路失败降级到 image-model（caption 标注「降级生成」） */
  degraded?: boolean
}

export interface ChatDelta {
  type: 'delta' | 'image' | 'done' | 'error'
  content?: string
  message?: string
  /** image 事件携带 */
  url?: string
  prompt?: string
  error?: string
  source?: string
  degraded?: boolean
}

export interface UploadResult {
  doc_id: string
  page_count: number
  en_pages?: number
  vision_pages?: number[]
  empty_pages?: number[]
  vision?: VisionStatus
}

export type ModelCapability = 'chat' | 'vision' | 'image_gen'

export interface ModelProvider {
  id: number
  name: string
  type: string
  base_url: string
  masked_key: string | null
  auth_type: string
  /** 敏感请求头的值由后端掩码；编辑时原样提交掩码不会覆盖旧 secret */
  extra_headers: Record<string, string>
  enabled: boolean
  note: string
  created_at?: string
  updated_at?: string
}

export interface ModelConfig {
  id: number
  provider_id: number
  provider_name?: string | null
  model_name: string
  capability: ModelCapability
  enabled: boolean
  priority: number
  params: Record<string, unknown>
  note: string
  created_at?: string
  updated_at?: string
}

export interface ModelTestResult {
  ok: boolean
  latency_ms?: number
  message: string
  sample_output?: string
  error_type?: 'auth_error' | 'model_not_found' | 'timeout' | 'network' | 'bad_response' | 'not_found'
}

/** 存档的对话记录（GET /api/conversations 返回项） */
export interface ConversationRecord {
  id: number
  doc_id: number
  pages: number[]
  messages: ChatMessage[]
  created_at: string
}
