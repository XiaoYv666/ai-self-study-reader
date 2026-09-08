// 中栏：PDF 连续滚动预览 + 点击勾选页（多选高亮）+ 缩放 + 当前页追踪
// react-pdf 堆叠 Page 实现连续滚动（技术文档 §2 关键决策）
// 页面宽高比：打开课件时拉 /api/docs/{id}/pages（document_pages.width/height，
//   每页实际 pt 尺寸）按页真实比例渲染 —— A4 竖版 / PPT 横版混排自适应；
//   接口失败/未返回时兜底 16:9（M1 行为），不崩
import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import { Document, Page } from 'react-pdf'
import type { DocumentNode, VisionStatus } from '../types'
import { getDocPages, rereadVisionPages } from '../lib/api'
import type { DocPageSize } from '../lib/api'
import { toast } from '../lib/toast'
import ConversationHistoryModal from './ConversationHistoryModal'

export interface PdfViewerHandle {
  scrollToPage: (p: number) => void
}

interface Props {
  doc: DocumentNode | null
  pdfSrc: string
  selectedPages: Set<number>
  onTogglePage: (p: number) => void
  onClearSelection: () => void
}

const BASE_WIDTH = 680

const PdfViewer = forwardRef<PdfViewerHandle, Props>(function PdfViewer(
  { doc, pdfSrc, selectedPages, onTogglePage, onClearSelection },
  ref,
) {
  const [numPages, setNumPages] = useState(0)
  const [zoom, setZoom] = useState(100)
  const [curPage, setCurPage] = useState(1)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [pageSizes, setPageSizes] = useState<DocPageSize[]>([])
  const [vision, setVision] = useState<VisionStatus>({ status: 'done', done: 0, total: 0, failed_pages: [] })
  const [historyPage, setHistoryPage] = useState<number | null>(null)
  const canvasRef = useRef<HTMLDivElement>(null)
  const pageEls = useRef<(HTMLDivElement | null)[]>([])

  const pageWidth = Math.round((BASE_WIDTH * zoom) / 100)
  const selectedCount = selectedPages.size

  useEffect(() => {
    // 切换课件时重置
    setNumPages(0)
    setCurPage(1)
    setLoadError(null)
    setPageSizes([])
    setVision(doc?.vision ?? { status: 'done', done: 0, total: 0, failed_pages: [] })
    if (canvasRef.current) canvasRef.current.scrollTop = 0
    if (!doc) return
    let cancelled = false
    let timer: number | null = null
    // 拉每页实际宽高 + source + vision 进度；pending/running 时 5s 轮询
    const pull = () => {
      getDocPages(doc.id)
        .then(({ pages: sizes, vision: nextVision }) => {
          if (cancelled) return
          if (sizes.length) setPageSizes(sizes)
          setVision(nextVision)
          if (nextVision.status === 'pending' || nextVision.status === 'running') {
            timer = window.setTimeout(pull, 5000)
          }
        })
        .catch(() => {
          /* 兜底 16:9，见 pageRatioFor */
        })
    }
    pull()
    return () => {
      cancelled = true
      if (timer != null) window.clearTimeout(timer)
    }
  }, [doc?.id, pdfSrc])

  /** page_no → 实际宽高，避免每页线性查找 */
  const sizeByPage = useMemo(() => {
    const m = new Map<number, DocPageSize>()
    for (const s of pageSizes) m.set(s.page_no, s)
    return m
  }, [pageSizes])

  /** 页面宽高比：按页实际 width/height（兜底 16/9），供 CSS aspect-ratio 使用 */
  const pageRatioFor = useCallback(
    (p: number) => {
      const s = sizeByPage.get(p)
      if (s && s.width > 0 && s.height > 0) {
        return s.width / s.height
      }
      return 16 / 9
    },
    [sizeByPage],
  )

  const isVisionBusy = vision.status === 'pending' || vision.status === 'running'
  const failedSet = useMemo(() => new Set(vision.failed_pages ?? []), [vision.failed_pages])

  const retryFailedPage = useCallback(async (p: number) => {
    if (!doc) return
    try {
      toast(`正在重试识别 P${p}…`)
      await rereadVisionPages(doc.id, [p])
      const next = await getDocPages(doc.id)
      setPageSizes(next.pages)
      setVision(next.vision)
      toast(next.vision.failed_pages.includes(p) ? `P${p} 仍识别失败` : `P${p} 已识别完成`)
    } catch (e) {
      toast(`重试失败：${e instanceof Error ? e.message : e}`)
    }
  }, [doc])

  const onScroll = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const line = canvas.scrollTop + 110
    let cur = 1
    pageEls.current.forEach((el, i) => {
      if (el && el.offsetTop <= line) cur = i + 1
    })
    setCurPage((prev) => (prev === cur ? prev : cur))
  }, [])

  const scrollToPage = useCallback((p: number) => {
    const canvas = canvasRef.current
    const el = pageEls.current[p - 1]
    if (!canvas || !el) return
    const target = el.offsetTop - canvas.clientHeight / 2 + el.clientHeight / 2
    canvas.scrollTo({ top: Math.max(0, target) })
    el.classList.remove('flash')
    void el.offsetWidth
    el.classList.add('flash')
    setCurPage(p)
  }, [])

  useImperativeHandle(ref, () => ({ scrollToPage }), [scrollToPage])

  const pages = useMemo(() => Array.from({ length: numPages }, (_, i) => i + 1), [numPages])

  if (!doc) {
    return (
      <main id="viewer">
        <div className="vw-toolbar" style={{ gap: 8 }}>
          <div className="vw-doc">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
              <polyline points="14 2 14 8 20 8" />
            </svg>
            <span className="t">未打开课件</span>
          </div>
          <div className="vw-spacer" />
        </div>
        <div className="canvas">
          <div className="placeholder">
            <div className="ph-ic">📄</div>
            <div className="ph-t">从左侧选择一份课件开始阅读</div>
            <div className="ph-s">
              点击页面即可「勾选」，勾选后可在右侧 AI 助教基于勾选页提问
              <br />
              支持上传 PDF / PPT / Word 课件
            </div>
          </div>
        </div>
      </main>
    )
  }

  return (
    <main id="viewer">
      <div className="vw-toolbar">
        <div className="vw-doc">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
          </svg>
          <span className="t">{doc.filename}</span>
          <span className="s">{doc.page_count} 页 · 双语课件</span>
          {isVisionBusy && (
            <span className="vision-pill" title="图片型页面正在后台 AI 识别，不影响阅读和提问">
              AI 识别中 {vision.done}/{vision.total}
            </span>
          )}
          {vision.status === 'partial' && vision.failed_pages.length > 0 && (
            <span className="vision-pill warn" title={`失败页：${vision.failed_pages.join('、')}`}>
              识别失败 {vision.failed_pages.length} 页
            </span>
          )}
        </div>
        <div className="vw-loc" title="当前阅读位置">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M1 6v16" />
            <path d="M1 6h13a3 3 0 0 1 3 3v13" />
            <path d="M1 10h8a2 2 0 0 1 2 2v10" />
          </svg>
          <span>
            当前页 <b>P{curPage}</b> / 共 <span>{numPages || doc.page_count}</span> 页
          </span>
        </div>
        <div className="vw-spacer" />
        <div className={`vw-sel${selectedCount ? ' on' : ''}`}>
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="20 6 9 17 4 12" />
          </svg>
          <span>已选 {selectedCount} 页</span>
          {selectedCount > 0 && (
            <span
              className="clr"
              title="清除选择"
              onClick={() => {
                onClearSelection()
                toast('已清除勾选')
              }}
            >
              ✕
            </span>
          )}
        </div>
        <button
          className="vw-hist"
          title={`查看 P${curPage} 页的历史对话`}
          aria-label={`查看 P${curPage} 页的历史对话`}
          onClick={() => setHistoryPage(curPage)}
        >
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10" />
            <polyline points="12 6 12 12 16 14" />
          </svg>
          <span>该页历史</span>
          <b>P{curPage}</b>
        </button>
        <div className="vw-tool-group">
          <button title="缩小" aria-label="缩小" onClick={() => setZoom((z) => Math.max(60, z - 10))}>
            −
          </button>
          <span className="vw-zoom">{zoom}%</span>
          <button title="放大" aria-label="放大" onClick={() => setZoom((z) => Math.min(150, z + 10))}>
            ＋
          </button>
        </div>
      </div>

      <div className="canvas" ref={canvasRef} onScroll={onScroll}>
        {loadError ? (
          <div className="placeholder">
            <div className="ph-ic">⚠️</div>
            <div className="ph-t">PDF 加载失败</div>
            <div className="ph-s">
              {loadError}
              <br />
              <button
                className="send-btn"
                style={{ marginTop: 14, height: 32, padding: '0 18px' }}
                onClick={() => {
                  setLoadError(null)
                  setNumPages(0)
                }}
              >
                重新加载
              </button>
            </div>
          </div>
        ) : (
          <Document
            file={pdfSrc}
            onLoadSuccess={({ numPages: n }) => {
              setNumPages(n)
              setLoadError(null)
            }}
            onLoadError={(e) => setLoadError(String(e?.message ?? e ?? '无法解析 PDF'))}
            loading={
              <div className="placeholder" style={{ marginTop: 30 }}>
                <div className="ph-ic">📄</div>
                <div className="ph-t">正在加载课件…</div>
              </div>
            }
          >
            {pages.map((p) => (
              <div
                key={p}
                ref={(el) => {
                  pageEls.current[p - 1] = el
                }}
                className={`page${selectedPages.has(p) ? ' selected' : ''}`}
                style={
                  {
                    '--pw': `${pageWidth}px`,
                    '--ratio': `${pageRatioFor(p)}`, // 按页真实宽高比，宽度自适应容器后高度自动跟随
                  } as CSSProperties
                }
                onClick={() => onTogglePage(p)}
                data-p={p}
              >
                <span className="corner">
                  <span className="ck">✓</span>
                </span>
                <span
                  className="page-num"
                  title="查看该页历史对话"
                  onClick={(e) => {
                    e.stopPropagation()
                    setHistoryPage(p)
                  }}
                >
                  P{p}
                </span>
                {sizeByPage.get(p)?.source === 'vision' && (
                  <span className="ai-badge" title="本页文本由 AI 识图补全">AI</span>
                )}
                {failedSet.has(p) && (
                  <button
                    className="ai-badge fail"
                    title="识别失败，点击重试"
                    onClick={(e) => {
                      e.stopPropagation()
                      void retryFailedPage(p)
                    }}
                  >
                    AI!
                  </button>
                )}
                <div className="pdf-inner">
                  <Page
                    pageNumber={p}
                    width={pageWidth}
                    renderTextLayer={false}
                    renderAnnotationLayer={false}
                    loading={<div className="pdf-loading">渲染中…</div>}
                  />
                </div>
              </div>
            ))}
          </Document>
        )}
      </div>

      <ConversationHistoryModal
        doc={doc}
        page={historyPage}
        onClose={() => setHistoryPage(null)}
      />
    </main>
  )
})

export default PdfViewer
