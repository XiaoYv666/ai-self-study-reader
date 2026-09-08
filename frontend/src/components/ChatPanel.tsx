// 右栏：AI 助教对话面板
// 流式：src/lib/api.ts chatStream() 逐段追加（真实后端 SSE / mock 打字机）
// 勾选页以 chips 展示，点击可滚动定位到对应 PDF 页
import { useEffect, useRef, useState } from 'react'
import { chatStream, fetchModelConfigs, fetchModelProviders, saveConversation } from '../lib/api'
import { createChatRunManager, shouldSendFromComposerKey } from '../lib/chatInteraction'
import { Markdown } from '../lib/markdown'
import { toast } from '../lib/toast'
import type { ChatImage, ChatMessage, DocumentNode, ModelConfig } from '../types'

interface Props {
  doc: DocumentNode | null
  selectedPages: Set<number>
  onRemovePage: (p: number) => void
  onClearPages: () => void
  onPageChipClick: (p: number) => void
  modelRefreshKey: number
}

const now = () =>
  new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })

// 图片卡片来源小标（克制：9px 角落标签，不抢视觉）
const SOURCE_LABEL: Record<string, string> = {
  plot: '精确绘制',
  waveform: '波形图',
  boolean: '布尔图',
  circuit: '电路图',
  draw: 'AI 生成',
}

export default function ChatPanel({ doc, selectedPages, onRemovePage, onClearPages, onPageChipClick, modelRefreshKey }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [saving, setSaving] = useState(false)
  const [chatModels, setChatModels] = useState<ModelConfig[]>([])
  const [selectedModelId, setSelectedModelId] = useState<number | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const runManager = useRef(createChatRunManager())

  const sel = [...selectedPages].sort((a, b) => a - b)
  const selectedModel = chatModels.find((m) => m.id === selectedModelId) ?? null
  const modelLabel = (m: ModelConfig) => `${m.model_name} · ${m.provider_name ?? `Provider #${m.provider_id}`}`

  useEffect(() => {
    let active = true
    const loadModels = async () => {
      const [configs, providers] = await Promise.all([fetchModelConfigs('chat'), fetchModelProviders()])
      const providerById = new Map(providers.map((provider) => [provider.id, provider]))
      const available = configs
        .filter((m) => {
          const provider = providerById.get(m.provider_id)
          return m.enabled && !!provider && provider.enabled
        })
        .sort((a, b) => a.priority - b.priority || a.id - b.id)
      if (!active) return
      setChatModels(available)
      setSelectedModelId((current) => available.some((m) => m.id === current) ? current : available[0]?.id ?? null)
    }
    void loadModels().catch((e) => {
      if (!active) return
      setChatModels([])
      setSelectedModelId(null)
      toast(`加载推理模型失败：${e instanceof Error ? e.message : e}`)
    })
    const onFocus = () => void loadModels().catch(() => undefined)
    window.addEventListener('focus', onFocus)
    return () => {
      active = false
      window.removeEventListener('focus', onFocus)
    }
  }, [modelRefreshKey])

  // 可存档的消息：过滤空 assistant 流式占位（欢迎语不进入 messages state，天然排除）
  const saveableMessages = messages
    .filter((m) => m.content.trim() || (m.images?.length ?? 0) > 0)
    .map((m) => ({ role: m.role, content: m.content.trim(), images: m.images }))
  const canSave = !!doc && selectedPages.size > 0 && saveableMessages.length > 0 && !streaming && !saving

  const saveTitle = !doc
    ? '请先在左侧打开课件'
    : selectedPages.size === 0
      ? '请先勾选课件页面'
      : saveableMessages.length === 0
        ? '还没有可保存的对话'
        : streaming
          ? '回答生成中，稍候再保存'
          : '将当前对话按勾选页存档'

  const handleSave = async () => {
    if (!doc || saving) return
    if (selectedPages.size === 0) {
      toast('请先勾选至少一页课件')
      return
    }
    if (saveableMessages.length === 0) {
      toast('还没有可保存的对话')
      return
    }
    setSaving(true)
    try {
      await saveConversation({ doc_id: Number(doc.id), pages: sel, messages: saveableMessages })
      toast('对话已保存')
    } catch (e) {
      toast(`保存失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setSaving(false)
    }
  }

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight
  }, [messages, streaming])

  useEffect(() => {
    return () => {
      runManager.current.stop()
    }
  }, [doc?.id])

  const stopGenerating = () => {
    runManager.current.stop()
    setStreaming(false)
  }

  const send = async () => {
    const text = input.trim()
    if (!text || streaming) return
    if (!doc) {
      toast('请先在左侧打开一份课件')
      return
    }
    if (selectedPages.size === 0) {
      toast('请先勾选至少一页课件，AI 将基于勾选页回答')
      return
    }
    if (!selectedModelId || !selectedModel) {
      toast('请前往 设置 → 模型与 API 配置并启用推理模型')
      return
    }
    const pages = [...selectedPages].sort((a, b) => a - b)
    const userMsg: ChatMessage = { role: 'user', content: text, pages }
    const history = messages.map((m) => ({ role: m.role, content: m.content }))
    const assistantMsg: ChatMessage = { role: 'assistant', content: '', model_label: modelLabel(selectedModel) }

    setMessages((ms) => [...ms, userMsg, assistantMsg])
    setInput('')
    setStreaming(true)
    if (inputRef.current) inputRef.current.style.height = '38px'
    const run = runManager.current.start()

    try {
      for await (const evt of chatStream(
        { doc_id: doc.id, pages, question: text, history, model_config_id: selectedModelId },
        { signal: run.controller.signal },
      )) {
        if (!runManager.current.isCurrent(run.id)) return
        if (evt.type === 'delta' && evt.content) {
          setMessages((ms) => {
            const copy = [...ms]
            const last = copy[copy.length - 1]
            if (last?.role === 'assistant') copy[copy.length - 1] = { ...last, content: last.content + evt.content }
            return copy
          })
        } else if (evt.type === 'image') {
          // P2：出图事件（正文流后到达）—— 追加图片卡片（精确绘制秒出，生图慢 15-40s）
          setMessages((ms) => {
            const copy = [...ms]
            const last = copy[copy.length - 1]
            if (last?.role === 'assistant') {
              const src = evt.source === 'plot' || evt.source === 'draw' || evt.source === 'circuit' || evt.source === 'boolean' || evt.source === 'waveform' ? evt.source : 'draw'
              const img: ChatImage = {
                url: evt.url ?? '',
                prompt: evt.prompt ?? '',
                error: evt.error,
                source: src,
                degraded: evt.degraded,
              }
              const prev = last.images ?? []
              copy[copy.length - 1] = { ...last, images: [...prev, img] }
            }
            return copy
          })
        } else if (evt.type === 'error') {
          setMessages((ms) => {
            const copy = [...ms]
            const last = copy[copy.length - 1]
            if (last?.role === 'assistant') {
              copy[copy.length - 1] = { ...last, content: `${last.content}\n\n> ⚠️ ${evt.message ?? '对话请求失败，请稍后重试'}` }
            }
            return copy
          })
          toast(evt.message ?? '对话请求失败')
          break
        } else if (evt.type === 'done') {
          break
        }
      }
    } finally {
      if (runManager.current.finish(run.id)) setStreaming(false)
    }
  }

  const newChat = () => {
    stopGenerating()
    setMessages([])
    onClearPages()
    toast('已开启新对话，请勾选页面后提问')
  }

  return (
    <section id="chat">
      <div className="chat-head">
        <div className="chat-title">
          AI 助教 <span className="live" />
          <span className="sub">耐心讲解 · 双语</span>
        </div>
        <div className="chat-actions">
          <button
            className="icon-btn"
            id="btnNew"
            title="新对话"
            aria-label="新对话"
            onClick={newChat}
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 5v14M5 12h14" />
            </svg>
          </button>
          <div className="model-wrap">
            <select
              id="modelSel"
              title={chatModels.length ? '本次对话使用的推理模型' : '未配置推理模型'}
              value={selectedModelId ?? ''}
              disabled={chatModels.length <= 1}
              onChange={(e) => {
                const id = Number(e.target.value)
                setSelectedModelId(id)
                const next = chatModels.find((m) => m.id === id)
                if (next) toast(`本次对话使用：${next.model_name}`)
              }}
            >
              {!chatModels.length && <option value="">未配置推理模型</option>}
              {chatModels.map((m) => (
                <option key={m.id} value={m.id}>
                  {modelLabel(m)}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      <div className="chat-scroll" ref={scrollRef}>
        {messages.length === 0 && !streaming && (
          <div className="msg ai">
            <div className="bubble">
              <div className="blk">
                <span className="blk-label">开始自学吧</span>
                <p>
                  我是你的 AI 助教。在中间预览区<b>点击勾选</b>课件的若干页，再在下方输入问题 —— 我会基于勾选页内容讲解，双语术语对照，公式按步骤推导。
                </p>
              </div>
            </div>
            <div className="meta">等待提问…</div>
          </div>
        )}

        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <div className="bubble">
              {m.role === 'user' ? (
                <>
                  {m.content}
                  {m.pages && m.pages.length > 0 && (
                    <>
                      {' '}
                      {m.pages.map((p) => (
                        <span key={p} className="pg-chip" onClick={() => onPageChipClick(p)}>
                          P{p}
                        </span>
                      ))}
                    </>
                  )}
                </>
              ) : m.content ? (
                <Markdown text={m.content} />
              ) : (
                <div className="typing">
                  <i />
                  <i />
                  <i />
                  <span className="caret" />
                </div>
              )}
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
            <div className="meta">
              {m.role === 'user' ? `基于勾选页 · ${now()}` : `${m.model_label ?? '推理模型'} · ${now()}`}
              {m.role === 'assistant' && m.content && <span className="svd">✓ 流式回答</span>}
            </div>
          </div>
        ))}
      </div>

      <div className={`context-strip${selectedPages.size ? ' on' : ''}`}>
        <span>{doc ? `提问将基于 ${doc.filename}` : '未打开课件'}</span>
        {sel.map((p) => (
          <span key={p} className="chip">
            P{p}
            <i className="x" onClick={() => onRemovePage(p)}>
              ✕
            </i>
          </span>
        ))}
        {selectedPages.size > 1 && (
          <span
            className="chip"
            style={{ cursor: 'pointer' }}
            onClick={() => {
              onClearPages()
              toast('已清除勾选')
            }}
          >
            清除全部
          </span>
        )}
        <button
          id="btnSave"
          className="save-btn"
          title={saveTitle}
          aria-label="保存本次对话"
          disabled={!canSave}
          onClick={() => void handleSave()}
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
            <polyline points="17 21 17 13 7 13 7 21" />
            <polyline points="7 3 7 8 15 8" />
          </svg>
          {saving ? '保存中…' : '保存本次对话'}
        </button>
      </div>

      <div className="chat-input">
        <textarea
          ref={inputRef}
          id="input"
          placeholder={doc ? '基于勾选的页面提问…（Enter 换行，Shift+Enter 发送）' : '先在左侧打开课件，勾选页面后提问…'}
          value={input}
          onChange={(e) => {
            setInput(e.target.value)
            e.target.style.height = '38px'
            e.target.style.height = Math.min(110, e.target.scrollHeight) + 'px'
          }}
          onKeyDown={(e) => {
            if (shouldSendFromComposerKey({
              key: e.key,
              shiftKey: e.shiftKey,
              isComposing: e.nativeEvent.isComposing,
            })) {
              e.preventDefault()
              void send()
            }
          }}
        />
        <button
          className="send-btn"
          id="sendBtn"
          title={streaming ? '停止生成' : '发送（Shift+Enter）'}
          aria-label={streaming ? '停止生成' : '发送'}
          disabled={!streaming && !input.trim()}
          onClick={streaming ? stopGenerating : () => void send()}
        >
          {streaming ? (
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <rect x="5" y="5" width="14" height="14" rx="1.5" />
            </svg>
          ) : (
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="22" y1="2" x2="11" y2="13" />
              <polygon points="22 2 15 22 11 13 2 9 22 2" />
            </svg>
          )}
          {streaming ? '停止' : '发送'}
        </button>
      </div>
    </section>
  )
}
