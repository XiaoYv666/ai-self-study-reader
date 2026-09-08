// 左栏：科目 / 文件夹 / 课件树（M3b：dnd-kit 拖拽排序 + 课件跨容器移动）
// 保留 M1 能力：搜索 / 折叠 / 新建科目 / 新建文件夹 / 上传；拖拽整理统一走 PATCH 落库，失败回滚
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  DndContext,
  DragOverlay,
  MeasuringStrategy,
  PointerSensor,
  closestCorners,
  pointerWithin,
  rectIntersection,
  useDroppable,
  useSensor,
  useSensors,
  type Collision,
  type CollisionDetection,
  type DragEndEvent,
  type DragOverEvent,
  type DragStartEvent,
} from '@dnd-kit/core'
import { SortableContext, arrayMove, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import type { ApiMode } from '../lib/api'
import { updateSubject, updateTreeMove } from '../lib/api'
import { applySidebarDrop, containerOfIn, itemDepthIn, subtreeHeightIn, validateUniqueChildItems } from '../lib/sidebarDrag'
import { toast } from '../lib/toast'
import type { DocumentNode, FolderNode, Subject, TreeResponse } from '../types'

const FileIcon = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
    <polyline points="14 2 14 8 20 8" />
  </svg>
)

const FolderIcon = (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
  </svg>
)

const PlusIcon = (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
    <line x1="12" y1="5" x2="12" y2="19" />
    <line x1="5" y1="12" x2="19" y2="12" />
  </svg>
)

const TrashIcon = (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
    <path d="M3 6h18" />
    <path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2" />
    <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
    <line x1="10" y1="11" x2="10" y2="17" />
    <line x1="14" y1="11" x2="14" y2="17" />
  </svg>
)

/** hover 行右侧的删除入口：普通状态隐藏（opacity:0），hover 行才出现；点击不触发行选中/拖拽 */
function RowDeleteButton({ title, disabled, onClick }: { title: string; disabled?: boolean; onClick: () => void }) {
  return (
    <button
      className="sb-del"
      title={title}
      aria-label={title}
      disabled={disabled}
      onClick={(e) => {
        e.stopPropagation()
        onClick()
      }}
      onPointerDown={(e) => e.stopPropagation()}
    >
      {TrashIcon}
    </button>
  )
}

/** 行内删除确认（参考 conv-confirm：文案 + 取消 / 暖红确认；不用 window.confirm） */
function DeleteConfirm({
  title,
  warn,
  busy,
  onConfirm,
  onCancel,
}: {
  title: React.ReactNode
  warn?: React.ReactNode
  busy: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  const boxRef = useRef<HTMLDivElement>(null)
  // 列表底部的行确认框可能渲染在视口外，用户看不到反馈以为「点了没反应」；
  // 挂载时把确认框滚入可视区（nearest：尽量少滚动，就地可见即可）
  useEffect(() => {
    boxRef.current?.scrollIntoView({ block: 'nearest' })
  }, [])
  return (
    <div
      ref={boxRef}
      className="sb-del-confirm"
      onClick={(e) => e.stopPropagation()}
      onPointerDown={(e) => e.stopPropagation()}
    >
      <div className="sb-del-warn">
        {title}
        {warn ? <> {warn}</> : null}
      </div>
      <div className="sb-del-btns">
        <button className="cancel" disabled={busy} onClick={onCancel}>
          取消
        </button>
        <button className="ok" disabled={busy} onClick={onConfirm}>
          {busy ? '删除中…' : '确认删除'}
        </button>
      </div>
    </div>
  )
}

/* ───────────── 拖拽 ID 约定 ─────────────
 * 项 ID：`sub:{id}` / `folder:{id}` / `doc:{id}`
 * 容器 ID（可拖入的空白区）：`cont:{containerKey}`
 * containerKey：
 *   subjects
 *   children:{subjectId}:root          科目根级文件夹/课件
 *   children:{subjectId}:{folderId}    某文件夹内子文件夹/课件
 */
type Kind = 'subject' | 'folder' | 'doc'

const kindOf = (itemId: string): Kind => (itemId.startsWith('sub:') ? 'subject' : (itemId.split(':')[0] as Kind))
const idOf = (itemId: string): string => itemId.slice(itemId.indexOf(':') + 1)
const keyOf = (folderId: string | null) => (folderId == null ? 'root' : folderId)
const childContainerKey = (subjectId: string, parentId: string | null) => `children:${subjectId}:${keyOf(parentId)}`
const subjectOfContainer = (ck: string) => ck.split(':')[1]
const parentOfContainer = (ck: string) => ck.split(':')[2]

interface DragState {
  items: Record<string, string[]>
  activeId: string
}

/** 一次整理动作的完整描述：乐观状态 + 落库 PATCH 列表（App 统一执行 / 失败回滚） */
export interface SidebarMutation {
  subjects?: Subject[]
  trees?: Record<string, TreeResponse>
  ops: (() => Promise<unknown>)[]
  refetchSubjects?: boolean
  refetchTrees?: string[]
  okMsg?: string
}

/** 行内确认后的删除请求（App 统一执行：调 API + 清理状态 + toast） */
export interface SidebarDeleteRequest {
  kind: 'subject' | 'folder' | 'doc'
  id: string
  subjectId?: string
  name: string
}

interface Props {
  subjects: Subject[]
  trees: Record<string, TreeResponse>
  activeSubjectId: string | null
  activeDocId: string | null
  collapsed: Record<string, boolean>
  mode: ApiMode
  onSelectSubject: (id: string) => void
  onToggleSubject: (id: string) => void
  onSelectDoc: (id: string) => void
  onCreateSubject: (name: string) => Promise<void>
  onCreateFolder: (subjectId: string, name: string) => Promise<void>
  onUpload: (file: File) => void
  onMutate: (m: SidebarMutation) => Promise<void>
  onDelete: (req: SidebarDeleteRequest) => Promise<boolean>
  onOpenModelSettings: () => void
}

/* ───────────── 可排序 / 可拖入 行组件 ───────────── */

function SortableDocRow({
  doc,
  active,
  dropTarget,
  onClick,
  confirmDelete,
  deleting,
  onRequestDelete,
  onConfirmDelete,
  onCancelDelete,
}: {
  doc: DocumentNode
  active: boolean
  dropTarget: boolean
  onClick: () => void
  confirmDelete: boolean
  deleting: boolean
  onRequestDelete: () => void
  onConfirmDelete: () => void
  onCancelDelete: () => void
}) {
  const { setNodeRef, transform, transition, isDragging, attributes, listeners } = useSortable({
    id: `doc:${doc.id}`,
    data: { kind: 'doc' as Kind },
  })
  if (confirmDelete) {
    return (
      <div ref={setNodeRef} style={{ transform: CSS.Transform.toString(transform), transition }} className={isDragging ? 'dragging' : ''}>
        <div className="sb-item confirm-mode" style={{ background: 'var(--paper)', boxShadow: 'var(--shadow-1)' }}>
          {FileIcon}
          <span className="name">{doc.filename.replace(/\.pdf$/i, '')}</span>
          <DeleteConfirm
            warn="将同时删除其页宽记录与全部对话存档，不可恢复"
            busy={deleting}
            title="删除此课件？"
            onConfirm={onConfirmDelete}
            onCancel={onCancelDelete}
          />
        </div>
      </div>
    )
  }
  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={`sb-item${active ? ' active' : ''}${dropTarget ? ' dnd-target' : ''}${isDragging ? ' dragging' : ''}`}
      onClick={onClick}
      title={doc.filename}
      {...attributes}
      {...listeners}
    >
      {FileIcon}
      <span className="name">{doc.filename.replace(/\.pdf$/i, '')}</span>
      <span className="pg">{doc.page_count} 页</span>
      <RowDeleteButton
        title={`删除课件「${doc.filename.replace(/\.pdf$/i, '')}」`}
        disabled={deleting}
        onClick={onRequestDelete}
      />
    </div>
  )
}

/** 拖动时才展开的明确同级插入线。 */
function InsertionDropZone({
  containerKey,
  index,
  enabled,
  active,
  rootEnd = false,
  dropId,
}: {
  containerKey: string
  index: number
  enabled: boolean
  active: boolean
  rootEnd?: boolean
  /** 自定义 droppable id（科目序列的插入线用 insert-subject: 前缀，避免与 children 容器冲突） */
  dropId?: string
}) {
  const id = dropId ?? `insert:${containerKey}:${index}`
  const { setNodeRef, isOver } = useDroppable({ id, disabled: !enabled })
  return (
    <div
      ref={setNodeRef}
      className={`sb-insert-zone${enabled ? ' visible' : ''}${active || isOver ? ' active' : ''}${rootEnd ? ' root-end' : ''}`}
      aria-hidden={!enabled}
    >
      <span>{rootEnd ? '放到科目根级' : '放这里'}</span>
    </div>
  )
}

function ChildContainer({ items, children }: { items: string[]; children: React.ReactNode }) {
  return (
    <div className="sb-docs-zone">
      <SortableContext items={items} strategy={verticalListSortingStrategy}>
        {children}
      </SortableContext>
    </div>
  )
}

/* ───────────── 科目块（含文件夹树 + 课件列表） ───────────── */

function FolderBlock(props: {
  folder: FolderNode
  subjectId: string
  viewItems: Record<string, string[]>
  docMap: Map<string, DocumentNode>
  folderMap: Map<string, FolderNode>
  activeDocId: string | null
  overTargetId: string | null
  activeKind: Kind | null
  activeIsDoc: boolean
  /** 正在拖拽的文件夹项（folder:<id>），用于深度校验的 enter 落点开关 */
  activeFolderItem: string | null
  onSelectDoc: (id: string) => void
  query: string
  confirmDeleteId: string | null
  deletingId: string | null
  requestDelete: (kind: Kind, id: string, subjectId: string, name: string) => void
  executeDelete: () => void
  cancelDelete: () => void
}) {
  const { folder, subjectId, viewItems, docMap, folderMap, activeDocId, overTargetId, activeKind, activeIsDoc, activeFolderItem, onSelectDoc, query, confirmDeleteId, deletingId, requestDelete, executeDelete, cancelDelete } = props
  const childIds = viewItems[childContainerKey(subjectId, folder.id)] ?? []
  const visibleChildIds = childIds.filter((itemId) => {
    if (kindOf(itemId) !== 'doc' || !query) return true
    return docMap.get(idOf(itemId))?.filename.toLowerCase().includes(query)
  })
  const enterId = `enter-folder:${folder.id}`
  const isOverTarget = overTargetId === enterId
  const confirming = confirmDeleteId === `folder:${folder.id}`
  const deleting = deletingId === `folder:${folder.id}`

  const { setNodeRef, transform, transition, isDragging, attributes, listeners } = useSortable({
    id: `folder:${folder.id}`,
    data: { kind: 'folder' as Kind },
  })
  // enter 落点：doc 始终可用；folder 仅当 深度合法（目标根级 + active 无子文件夹 + 非自身子树）时可用。
  const folderEnterOk = activeFolderItem !== null
    && itemDepthIn(viewItems, `folder:${folder.id}`) === 1
    && subtreeHeightIn(viewItems, activeFolderItem) === 1
    && activeFolderItem !== `folder:${folder.id}`
  const { setNodeRef: setEnterRef, isOver: isEnterOver } = useDroppable({ id: enterId, disabled: !activeIsDoc && !folderEnterOk })
  const entering = activeIsDoc || folderEnterOk ? (isOverTarget || isEnterOver) : false

  if (confirming) {
    return (
      <div ref={setNodeRef} style={{ transform: CSS.Transform.toString(transform), transition }} className={isDragging ? 'dragging' : ''}>
        <div className="sb-folder confirm-mode" style={{ background: 'var(--paper)', boxShadow: 'var(--shadow-1)', borderRadius: 'var(--radius)' }}>
          {FolderIcon}
          <span className="name">{folder.name}</span>
          <DeleteConfirm
            warn="需先清空内容（仅空文件夹可删）"
            busy={deleting}
            title="删除此文件夹？"
            onConfirm={executeDelete}
            onCancel={cancelDelete}
          />
        </div>
        {/* 确认期间子项保持原位渲染（不参与交互） */}
        <div style={{ paddingLeft: 10, opacity: .5, pointerEvents: 'none' }}>
          <ChildContainer items={childIds}>
            {visibleChildIds.map((itemId) => {
              const f = folderMap.get(idOf(itemId))
              const d = docMap.get(idOf(itemId))
              return f ? (
                <div key={itemId} className="sb-item">{FolderIcon}<span className="name">{f.name}</span></div>
              ) : d ? (
                <div key={itemId} className="sb-item">{FileIcon}<span className="name">{d.filename.replace(/\.pdf$/i, '')}</span></div>
              ) : null
            })}
          </ChildContainer>
        </div>
      </div>
    )
  }

  return (
    <div ref={setNodeRef} style={{ transform: CSS.Transform.toString(transform), transition }} className={isDragging ? 'dragging' : ''}>
      <div ref={setEnterRef} className={`sb-folder${entering ? ' dnd-target' : ''}`} {...attributes} {...listeners}>
        {FolderIcon}
        <span className="name">{folder.name}</span>
        {entering && <span className="dnd-badge">移入</span>}
        <span className="docs-in">{childIds.length} 项</span>
        <RowDeleteButton
          title={`删除文件夹「${folder.name}」`}
          disabled={deleting}
          onClick={() => requestDelete('folder', folder.id, subjectId, folder.name)}
        />
      </div>
      <div style={{ paddingLeft: 10 }}>
        <ChildContainer items={childIds}>
          {visibleChildIds.map((itemId, visibleIndex) => {
            const actualIndex = childIds.indexOf(itemId)
            const insertId = `insert:${childContainerKey(subjectId, folder.id)}:${actualIndex}`
            return (
              <div key={itemId}>
                <InsertionDropZone
                  containerKey={childContainerKey(subjectId, folder.id)}
                  index={actualIndex}
                  enabled={activeKind === 'folder' || activeKind === 'doc'}
                  active={overTargetId === insertId}
                />
                {kindOf(itemId) === 'folder' ? (() => {
                  const f = folderMap.get(idOf(itemId))
                  return f ? <FolderBlock folder={f} subjectId={subjectId} viewItems={viewItems} docMap={docMap} folderMap={folderMap} activeDocId={activeDocId} overTargetId={overTargetId} activeKind={activeKind} activeIsDoc={activeIsDoc} activeFolderItem={activeFolderItem} onSelectDoc={onSelectDoc} query={query} confirmDeleteId={confirmDeleteId} deletingId={deletingId} requestDelete={requestDelete} executeDelete={executeDelete} cancelDelete={cancelDelete} /> : null
                })() : (() => {
                  const d = docMap.get(idOf(itemId))
                  return d ? (
                    <SortableDocRow
                      doc={d}
                      active={d.id === activeDocId}
                      dropTarget={false}
                      onClick={() => onSelectDoc(d.id)}
                      confirmDelete={confirmDeleteId === `doc:${d.id}`}
                      deleting={deletingId === `doc:${d.id}`}
                      onRequestDelete={() => requestDelete('doc', d.id, subjectId, d.filename)}
                      onConfirmDelete={executeDelete}
                      onCancelDelete={cancelDelete}
                    />
                  ) : null
                })()}
                {visibleIndex === visibleChildIds.length - 1 && (
                  <InsertionDropZone
                    containerKey={childContainerKey(subjectId, folder.id)}
                    index={childIds.length}
                    enabled={activeKind === 'folder' || activeKind === 'doc'}
                    active={overTargetId === `insert:${childContainerKey(subjectId, folder.id)}:${childIds.length}`}
                  />
                )}
              </div>
            )
          })}
          {visibleChildIds.length === 0 && (
            <InsertionDropZone
              containerKey={childContainerKey(subjectId, folder.id)}
              index={0}
              enabled={activeKind === 'folder' || activeKind === 'doc'}
              active={overTargetId === `insert:${childContainerKey(subjectId, folder.id)}:0`}
            />
          )}
        </ChildContainer>
      </div>
    </div>
  )
}

function SubjectBlock(props: {
  subject: Subject
  viewItems: Record<string, string[]>
  docMap: Map<string, DocumentNode>
  folderMap: Map<string, FolderNode>
  collapsed: boolean
  activeSubjectId: string | null
  activeDocId: string | null
  overTargetId: string | null
  activeKind: Kind | null
  activeIsDoc: boolean
  activeFolderItem: string | null
  addingFolderFor: string | null
  folderName: string
  setAddingFolderFor: (v: string | null) => void
  setFolderName: (v: string) => void
  onSelectSubject: (id: string) => void
  onToggleSubject: (id: string) => void
  onSelectDoc: (id: string) => void
  onCreateFolder: (subjectId: string, name: string) => Promise<void>
  query: string
  confirmDeleteId: string | null
  deletingId: string | null
  requestDelete: (kind: Kind, id: string, subjectId: string, name: string) => void
  executeDelete: () => void
  cancelDelete: () => void
}) {
  const {
    subject: s,
    viewItems,
    docMap,
    folderMap,
    collapsed: isCollapsed,
    activeSubjectId,
    activeDocId,
    overTargetId,
    activeKind,
    activeIsDoc,
    activeFolderItem,
    addingFolderFor,
    folderName,
    setAddingFolderFor,
    setFolderName,
    onSelectSubject,
    onToggleSubject,
    onSelectDoc,
    onCreateFolder,
    query,
    confirmDeleteId,
    deletingId,
    requestDelete,
    executeDelete,
    cancelDelete,
  } = props
  const active = s.id === activeSubjectId
  const rootIds = viewItems[childContainerKey(s.id, null)] ?? []
  const visibleRootIds = rootIds.filter((itemId) => {
    if (kindOf(itemId) !== 'doc' || !query) return true
    return docMap.get(idOf(itemId))?.filename.toLowerCase().includes(query)
  })
  const isOverTarget = (activeIsDoc || activeKind === 'folder') && overTargetId === `sub:${s.id}`

  const { setNodeRef, transform, transition, isDragging, attributes, listeners } = useSortable({
    id: `sub:${s.id}`,
    data: { kind: 'subject' as Kind },
  })
  // 科目标题行同时是显式接收落点：doc/folder 落到标题 = 移到该科目根级末尾。
  // 不依赖 sortable 自身的 over（会被源科目大块矩形抢走），注册独立 droppable 保证命中。
  const { setNodeRef: setSubDropRef, isOver: isSubDropOver } = useDroppable({
    id: `to-subject-root:${s.id}`,
    disabled: activeKind !== 'doc' && activeKind !== 'folder',
  })
  const subDropHi = !isDragging && (isOverTarget || isSubDropOver)

  const confirmingSubject = confirmDeleteId === `sub:${s.id}`
  const deletingSubject = deletingId === `sub:${s.id}`

  if (confirmingSubject) {
    return (
      <div ref={setNodeRef} style={{ transform: CSS.Transform.toString(transform), transition }} className={`sb-group${isDragging ? ' dragging' : ''}`}>
        <div
          className="sb-subject confirm-mode"
          style={{ background: 'var(--paper)', boxShadow: 'var(--shadow-1)', borderRadius: 'var(--radius)', padding: '6px 8px' }}
        >
          <span className="caret">▼</span>
          {s.name}
          <DeleteConfirm
            warn="仅空科目可删，需先移走内容"
            busy={deletingSubject}
            title="删除此科目？"
            onConfirm={executeDelete}
            onCancel={cancelDelete}
          />
        </div>
        {/* 确认期间子树保持原位渲染（不参与交互） */}
        <div className="sb-docs" style={{ opacity: .5, pointerEvents: 'none' }}>
          <ChildContainer items={rootIds}>
            {visibleRootIds.map((itemId) => {
              const f = folderMap.get(idOf(itemId))
              const d = docMap.get(idOf(itemId))
              return f ? (
                <div key={itemId} className="sb-item">{FolderIcon}<span className="name">{f.name}</span></div>
              ) : d ? (
                <div key={itemId} className="sb-item">{FileIcon}<span className="name">{d.filename.replace(/\.pdf$/i, '')}</span></div>
              ) : null
            })}
          </ChildContainer>
        </div>
      </div>
    )
  }

  return (
    <div ref={setNodeRef} style={{ transform: CSS.Transform.toString(transform), transition }} className={`sb-group${isCollapsed ? ' closed' : ''}${isDragging ? ' dragging' : ''}`}>
      <div
        ref={setSubDropRef}
        className={`sb-subject${subDropHi ? ' dnd-target' : ''}`}
        onClick={() => {
          onSelectSubject(s.id)
          if (isCollapsed) onToggleSubject(s.id)
        }}
        title={active ? '点击折叠 / 展开' : '展开该科目'}
        {...attributes}
        {...listeners}
      >
        <span className="caret">▼</span>
        {s.name}
        {subDropHi && <span className="dnd-badge">移入</span>}
        <button
          className="sb-add"
          title="新建文件夹"
          aria-label="新建文件夹"
          style={{ marginLeft: 2 }}
          onClick={(e) => {
            e.stopPropagation()
            setAddingFolderFor(addingFolderFor === s.id ? null : s.id)
            setFolderName('')
          }}
          onPointerDown={(e) => e.stopPropagation()}
        >
          {PlusIcon}
        </button>
        <span className="cnt">{s.doc_count} 份课件</span>
        <RowDeleteButton
          title={`删除科目「${s.name}」`}
          disabled={deletingSubject}
          onClick={() => requestDelete('subject', s.id, s.id, s.name)}
        />
      </div>
      <div className="sb-docs">
        {addingFolderFor === s.id && (
          <div className="sb-item" style={{ background: 'var(--paper)', boxShadow: 'var(--shadow-1)' }}>
            <input
              autoFocus
              value={folderName}
              placeholder="文件夹名，如「第一章」"
              style={{ flex: 1, border: '1px solid var(--border-hi)', borderRadius: 6, padding: '5px 8px', fontSize: 12.5, background: 'var(--paper)' }}
              onChange={(e) => setFolderName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && folderName.trim()) {
                  void onCreateFolder(s.id, folderName.trim()).then(() => setAddingFolderFor(null))
                }
                if (e.key === 'Escape') setAddingFolderFor(null)
              }}
            />
          </div>
        )}
        <ChildContainer items={rootIds}>
          {visibleRootIds.map((itemId) => {
            const actualIndex = rootIds.indexOf(itemId)
            return (
              <div key={itemId}>
                <InsertionDropZone
                  containerKey={childContainerKey(s.id, null)}
                  index={actualIndex}
                  enabled={activeKind === 'folder' || activeKind === 'doc'}
                  active={overTargetId === `insert:${childContainerKey(s.id, null)}:${actualIndex}`}
                />
                {kindOf(itemId) === 'folder' ? (() => {
                  const f = folderMap.get(idOf(itemId))
                  return f ? <FolderBlock folder={f} subjectId={s.id} viewItems={viewItems} docMap={docMap} folderMap={folderMap} activeDocId={activeDocId} overTargetId={overTargetId} activeKind={activeKind} activeIsDoc={activeIsDoc} activeFolderItem={activeFolderItem} onSelectDoc={onSelectDoc} query={query} confirmDeleteId={confirmDeleteId} deletingId={deletingId} requestDelete={requestDelete} executeDelete={executeDelete} cancelDelete={cancelDelete} /> : null
                })() : (() => {
                  const d = docMap.get(idOf(itemId))
                  return d ? (
                    <SortableDocRow
                      doc={d}
                      active={d.id === activeDocId}
                      dropTarget={false}
                      onClick={() => onSelectDoc(d.id)}
                      confirmDelete={confirmDeleteId === `doc:${d.id}`}
                      deleting={deletingId === `doc:${d.id}`}
                      onRequestDelete={() => requestDelete('doc', d.id, s.id, d.filename)}
                      onConfirmDelete={executeDelete}
                      onCancelDelete={cancelDelete}
                    />
                  ) : null
                })()}
              </div>
            )
          })}
          <InsertionDropZone
            containerKey={childContainerKey(s.id, null)}
            index={rootIds.length}
            enabled={activeKind === 'folder' || activeKind === 'doc'}
            active={overTargetId === `insert:${childContainerKey(s.id, null)}:${rootIds.length}`}
            rootEnd
          />
        </ChildContainer>
      </div>
    </div>
  )
}

/* ───────────── 主组件 ───────────── */

export default function SubjectSidebar(props: Props) {
  const { subjects, trees, activeSubjectId, activeDocId, collapsed, mode } = props
  const [q, setQ] = useState('')
  const [addingSubject, setAddingSubject] = useState(false)
  const [subjectName, setSubjectName] = useState('')
  const [addingFolderFor, setAddingFolderFor] = useState<string | null>(null)
  const [folderName, setFolderName] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  // —— 删除（行内确认 + 级联提示） ——
  /** 正在行内确认删除的项 ID（`sub:{id}` / `folder:{id}` / `doc:{id}`） */
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)
  /** 正在执行 DELETE 的项 ID（置灰防重复点击） */
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const pendingDeleteRef = useRef<SidebarDeleteRequest | null>(null)

  const requestDelete = useCallback((kind: Kind, id: string, subjectId: string, name: string) => {
    // 拖拽 ID 约定里科目是 `sub:{id}`（非 subject:），confirm 比对处统一用该前缀
    const kindPrefix = kind === 'subject' ? 'sub' : kind
    pendingDeleteRef.current = { kind, id, subjectId: kind === 'subject' ? undefined : subjectId, name }
    setConfirmDeleteId(`${kindPrefix}:${id}`)
  }, [])

  const cancelDelete = useCallback(() => {
    pendingDeleteRef.current = null
    setConfirmDeleteId(null)
  }, [])

  const executeDelete = useCallback(() => {
    const req = pendingDeleteRef.current
    if (!req) {
      setConfirmDeleteId(null)
      return
    }
    setDeletingId(`${req.kind === 'subject' ? 'sub' : req.kind}:${req.id}`)
    void props
      .onDelete(req)
      .then((ok) => {
        if (ok) setConfirmDeleteId(null)
        // 失败（含 409）：保持确认行展开，App 已 toast 引导文案，用户可取消或重试
      })
      .finally(() => setDeletingId(null))
  }, [])

  // —— M3b 拖拽状态 ——
  const [dragState, setDragState] = useState<DragState | null>(null)
  const [overId, setOverId] = useState<string | null>(null)
  const [pendingMove, setPendingMove] = useState<{ kind: 'doc' | 'folder'; id: string; fromSid: string; toSid: string; name: string } | null>(null)

  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 7 } }))

  const query = q.trim().toLowerCase()

  /* ── 查找表（key 统一为字符串；后端 id 为数字） ── */
  const subjectMap = useMemo(() => new Map(subjects.map((s) => [String(s.id), s])), [subjects])
  const docMap = useMemo(() => {
    const m = new Map<string, DocumentNode>()
    for (const t of Object.values(trees)) for (const d of t.documents) m.set(String(d.id), d)
    return m
  }, [trees])
  const folderMap = useMemo(() => {
    const m = new Map<string, FolderNode>()
    for (const t of Object.values(trees)) for (const f of t.folders) m.set(String(f.id), f)
    return m
  }, [trees])

  /* ── 容器 → 文件夹/课件统一序列（sort_order 冲突时按类型、id 稳定兜底） ── */
  const baseItems = useMemo(() => {
    const items: Record<string, string[]> = { subjects: subjects.map((s) => `sub:${s.id}`) }
    for (const s of subjects) {
      const tree = trees[s.id] ?? { folders: [], documents: [] }
      const parents = [null, ...tree.folders.map((f) => f.id)]
      for (const parentId of parents) {
        const mixed = [
          ...tree.folders.filter((f) => f.parent_id === parentId).map((f) => ({ id: `folder:${f.id}`, order: f.sort_order ?? 0, typeOrder: 0 })),
          ...tree.documents.filter((d) => d.folder_id === parentId).map((d) => ({ id: `doc:${d.id}`, order: d.sort_order ?? 0, typeOrder: 1 })),
        ].sort((a, b) => a.order - b.order || a.typeOrder - b.typeOrder || idOf(a.id).localeCompare(idOf(b.id), undefined, { numeric: true }))
        items[childContainerKey(s.id, parentId)] = mixed.map((x) => x.id)
      }
    }
    return items
  }, [subjects, trees])

  const viewItems = dragState?.items ?? baseItems
  const activeKind = dragState ? kindOf(dragState.activeId) : null
  const activeIsDoc = activeKind === 'doc'
  // overTargetId 同时驱动「移入」徽标与插入线高亮：doc 的徽标逻辑保留，
  // folder/subject 的插入线也用 overId 高亮（不再仅限 doc）。
  const overTargetId = overId && (activeIsDoc || overId.startsWith('insert')) ? overId : null

  /* ── 拖拽事件 ── */
  const handleDragStart = (e: DragStartEvent) => {
    const id = String(e.active.id)
    setDragState({ items: baseItems, activeId: id })
    setOverId(null)
    setPendingMove(null)
  }

  const handleDragOver = (e: DragOverEvent) => {
    const { active, over } = e
    if (!over) return
    const a = String(active.id)
    const o = String(over.id)
    setOverId(o)
    if (a === o) return
    // React 可能在连续 onDragOver 之间尚未重渲染；始终使用函数式更新拿到同一帧快照。
    setDragState((current) => {
      if (!current || current.activeId !== a) return current
      const items = current.items
      const ak = kindOf(a)

      if (ak === 'subject') {
        // 科目拖动 = 仅预览模式：悬停只更新插入线（overId 已在顶部设置），
        // 不做乐观重排——科目块很高（含整棵课件树），悬停即交换会导致
        // 布局震荡 → over 反复翻转 → 无限重渲染（Maximum update depth）。
        // 最终顺序在 handleDragEnd 一次性计算。
        return current
      }

      const ac = containerOfIn(items, a)
      if (!ac) return current

      // 文件夹和课件都只由明确落点驱动：插入线 = 同级；文件夹中心 = 移入；科目标题 = 该科目根级末尾。
      let dropId = o
      if (o.startsWith('to-subject-root:')) {
        const sid = o.slice('to-subject-root:'.length)
        const root = childContainerKey(sid, null)
        dropId = `insert:${root}:${items[root]?.length ?? 0}`
      } else if (kindOf(o) === 'subject') {
        if (ak === 'doc' || ak === 'folder') {
          dropId = `insert:${childContainerKey(idOf(o), null)}:${items[childContainerKey(idOf(o), null)]?.length ?? 0}`
        }
      }
      const nextItems = applySidebarDrop(items, a, dropId)
      return nextItems === items ? current : { ...current, items: nextItems }
    })
  }

  const handleDragCancel = () => {
    setDragState(null)
    setOverId(null)
  }

  /** 从 dragState 构建提交计划：乐观 subjects/trees + 落库 ops */
  const buildPlan = (state: DragState): SidebarMutation => {
    const items = state.items
    validateUniqueChildItems(items)
    // 完整性断言仅在 children 容器有实际变更时执行（纯科目排序不触碰任何 children key）。
    const baseSig = Object.entries(baseItems)
      .filter(([ck]) => ck.startsWith('children:'))
      .map(([ck, list]) => `${ck}=${list.join(',')}`)
      .sort()
      .join(';')
    const gotSig = Object.entries(items)
      .filter(([ck]) => ck.startsWith('children:'))
      .map(([ck, list]) => `${ck}=${list.join(',')}`)
      .sort()
      .join(';')
    const childrenChanged = baseSig !== gotSig
    const expectedItems = new Set(Object.entries(baseItems)
      .filter(([ck]) => ck.startsWith('children:'))
      .flatMap(([, list]) => list))
    const gotItems = new Set(Object.entries(items)
      .filter(([ck]) => ck.startsWith('children:'))
      .flatMap(([, list]) => list))
    if (childrenChanged && (expectedItems.size !== gotItems.size || [...expectedItems].some((itemId) => !gotItems.has(itemId)))) {
      throw new Error('拖拽状态无效：文件夹或课件集合不完整，已取消保存')
    }
    const ops: (() => Promise<unknown>)[] = []
    const newTrees: Record<string, TreeResponse> = {}
    let subjectsChanged = false
    const newSubjects: Subject[] = []

    // 科目排序
    const subIds = items.subjects ?? []
    if (subIds.join() !== subjects.map((s) => `sub:${s.id}`).join()) {
      subjectsChanged = true
      subIds.forEach((id, i) => {
        const s = subjectMap.get(idOf(id))
        if (s) {
          newSubjects.push({ ...s, sort_order: i })
          if (s.sort_order !== i) ops.push(() => updateSubject(s.id, { sort_order: i }))
        }
      })
    }

    // 统一容器序列生成乐观树；课件先更新归属，再由批量接口原子归一化混排 sort_order。
    const docOwner = new Map<string, string>()
    for (const [sid, tree] of Object.entries(trees)) for (const d of tree.documents) docOwner.set(d.id, sid)

    for (const s of subjects) {
      const tree = trees[s.id] ?? { folders: [], documents: [] }
      const newFolders: FolderNode[] = []
      const newDocs: DocumentNode[] = []
      const parentIds = [null, ...tree.folders.map((f) => f.id)]
      let changed = false

      for (const parentId of parentIds) {
        const ck = childContainerKey(s.id, parentId)
        const list = items[ck] ?? []
        const oldList = baseItems[ck] ?? []
        if (list.join() !== oldList.join()) changed = true

        for (const [index, itemId] of list.entries()) {
          if (kindOf(itemId) === 'folder') {
            const f = folderMap.get(idOf(itemId))
            // 跨容器移动文件夹时同步乐观 parent_id（容器即新父级）
            if (f) newFolders.push({ ...f, parent_id: parentId, sort_order: index })
          } else {
            const d = docMap.get(idOf(itemId))
            if (!d) continue
            newDocs.push({ ...d, folder_id: parentId, sort_order: index })
          }
        }
      }

      // 加入从其他科目拖入的课件；移出的课件从当前树删除。
      for (const [ck, list] of Object.entries(items)) {
        if (!ck.startsWith(`children:${s.id}:`)) continue
        const parentKey = parentOfContainer(ck)
        const parentId = parentKey === 'root' ? null : parentKey
        for (const [index, itemId] of list.entries()) {
          if (kindOf(itemId) !== 'doc') continue
          const d = docMap.get(idOf(itemId))
          if (d && docOwner.get(d.id) !== s.id && !newDocs.some((x) => x.id === d.id)) {
            newDocs.push({ ...d, folder_id: parentId, sort_order: index })
            changed = true
          }
        }
      }
      if (newDocs.length !== tree.documents.length) changed = true
      if (changed) newTrees[s.id] = { folders: newFolders, documents: newDocs }
    }

    if (kindOf(state.activeId) !== 'subject') {
      const sourceCk = containerOfIn(baseItems, state.activeId)
      const targetCk = containerOfIn(items, state.activeId)
      if (!sourceCk || !targetCk) throw new Error('拖拽状态无效：找不到源或目标容器')
      const affected = new Set([sourceCk, targetCk])
      for (const [ck, list] of Object.entries(items)) {
        if (ck.startsWith('children:') && list.join() !== (baseItems[ck] ?? []).join()) affected.add(ck)
      }
      const targetParent = parentOfContainer(targetCk)
      ops.push(() => updateTreeMove({
        active: {
          type: kindOf(state.activeId) === 'folder' ? 'folder' : 'document',
          id: idOf(state.activeId),
        },
        to_subject_id: subjectOfContainer(targetCk),
        to_parent_id: targetParent === 'root' ? null : targetParent,
        containers: [...affected].map((ck) => {
          const parent = parentOfContainer(ck)
          return {
            subject_id: subjectOfContainer(ck),
            parent_id: parent === 'root' ? null : parent,
            items: (items[ck] ?? []).map((itemId) => ({
              type: kindOf(itemId) === 'folder' ? 'folder' : 'document',
              id: idOf(itemId),
            })),
          }
        }),
      }))
    }

    return {
      subjects: subjectsChanged ? newSubjects : undefined,
      trees: Object.keys(newTrees).length ? newTrees : undefined,
      ops,
      okMsg: '已保存整理',
    }
  }

  const handleDragEnd = (e: DragEndEvent) => {
    const state = dragState
    const { over } = e
    setOverId(null)
    if (!state) {
      setDragState(null)
      return
    }
    // 科目拖动（预览模式）：释放时根据 over 目标一次性计算新顺序
    if (kindOf(state.activeId) === 'subject') {
      const list = state.items.subjects ?? []
      const from = list.indexOf(state.activeId)
      let insertAt: number | null = null
      if (over) {
        const o = String(over.id)
        if (o.startsWith('insert-subject:')) {
          const rest = o.slice('insert-subject:'.length)
          const sep = rest.lastIndexOf(':')
          const idx = Number(rest.slice(sep + 1))
          if (sep > 0 && Number.isInteger(idx) && idx >= 0) insertAt = idx
        } else if (kindOf(o) === 'subject' && o !== state.activeId) {
          // 兜底：直接释放在某科目块上 → 插到它前面
          const oi = list.indexOf(o)
          if (oi >= 0) insertAt = oi
        }
      }
      if (from >= 0 && insertAt !== null) {
        const adj = from < insertAt ? insertAt - 1 : insertAt
        const removed = list.filter((x) => x !== state.activeId)
        if (adj >= 0 && adj <= removed.length) {
          const next = [...removed.slice(0, adj), state.activeId, ...removed.slice(adj)]
          if (next.join() !== list.join()) {
            const newSubjects: Subject[] = []
            const ops: (() => Promise<unknown>)[] = []
            next.forEach((id, i) => {
              const s = subjectMap.get(idOf(id))
              if (s) {
                newSubjects.push({ ...s, sort_order: i })
                if (s.sort_order !== i) ops.push(() => updateSubject(s.id, { sort_order: i }))
              }
            })
            setDragState(null)
            void props.onMutate({ subjects: newSubjects, ops, okMsg: '已调整科目顺序' })
            return
          }
        }
      }
      setDragState(null)
      return
    }
    // 检测跨科目移动（课件 + 文件夹）→ 弹确认
    const items = state.items
    let cross: { kind: 'doc' | 'folder'; id: string; fromSid: string; toSid: string; name: string } | null = null
    // 课件：任一树里的 doc 出现在了其他科目的容器里
    for (const [sid, t] of Object.entries(trees)) {
      for (const d of t.documents) {
        const nck = Object.entries(items).find(([, list]) => list.includes(`doc:${d.id}`))?.[0]
        if (!nck || !nck.startsWith('children:')) continue
        const ns = subjectOfContainer(nck)
        if (ns !== sid) {
          cross = { kind: 'doc', id: d.id, fromSid: sid, toSid: ns, name: d.filename }
          break
        }
      }
      if (cross) break
    }
    // 文件夹：任一树里的 folder 出现在了其他科目的容器里
    if (!cross) {
      for (const [sid, t] of Object.entries(trees)) {
        for (const f of t.folders) {
          const nck = Object.entries(items).find(([, list]) => list.includes(`folder:${f.id}`))?.[0]
          if (!nck || !nck.startsWith('children:')) continue
          const ns = subjectOfContainer(nck)
          if (ns !== sid) {
            cross = { kind: 'folder', id: f.id, fromSid: sid, toSid: ns, name: f.name }
            break
          }
        }
        if (cross) break
      }
    }
    if (cross) {
      setPendingMove(cross)
      return // 等待确认，dragState 保留
    }
    let plan: SidebarMutation
    try {
      plan = buildPlan(state)
    } catch (err) {
      toast(err instanceof Error ? err.message : '拖拽状态无效，已取消保存')
      setDragState(null)
      setOverId(null)
      return
    }
    setDragState(null)
    void props.onMutate(plan)
  }

  const confirmCrossMove = () => {
    if (!pendingMove || !dragState) return
    const { fromSid, toSid } = pendingMove
    let plan: SidebarMutation
    try {
      plan = buildPlan(dragState)
    } catch (err) {
      toast(err instanceof Error ? err.message : '拖拽状态无效，已取消保存')
      setPendingMove(null)
      setDragState(null)
      setOverId(null)
      return
    }
    plan.refetchSubjects = true
    plan.refetchTrees = [fromSid, toSid]
    plan.okMsg = `已移至「${subjectMap.get(toSid)?.name ?? ''}」`
    setPendingMove(null)
    setDragState(null)
    void props.onMutate(plan)
  }

  const cancelPendingMove = () => {
    setPendingMove(null)
    setDragState(null)
    setOverId(null)
  }

  /* ── 搜索过滤（保留 M1 逻辑） ── */
  const visibleSubjects = subjects.filter((s) => {
    if (!query) return true
    if (s.name.toLowerCase().includes(query)) return true
    const tree = trees[s.id]
    if (!tree) return false
    return tree.documents.some((d) => d.filename.toLowerCase().includes(query))
  })
  const docVisible = (name: string) => !query || name.toLowerCase().includes(query)

  return (
    <aside id="sidebar">
      <div className="sb-head">
        <div className="logo"><img src="/ai-logo.svg" alt="" /></div>
        <div className="brand">AI 自学阅读器</div>
        {mode === 'mock' && <span className="sb-mode" title="后端未连接，使用内置演示数据">演示</span>}
      </div>

      <div className="sb-search">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <circle cx="11" cy="11" r="7" />
          <line x1="21" y1="21" x2="16.5" y2="16.5" />
        </svg>
        <input type="text" placeholder="搜索科目、课件…" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>

      <div className="sb-scroll">
        <div className="sb-section">
          <span>科目</span>
          <button
            className="sb-add"
            title="新建科目"
            aria-label="新建科目"
            onClick={() => {
              setAddingSubject(true)
              setSubjectName('')
            }}
          >
            {PlusIcon}
          </button>
        </div>

        {addingSubject && (
          <div className="sb-item" style={{ background: 'var(--paper)', boxShadow: 'var(--shadow-1)' }}>
            <input
              autoFocus
              value={subjectName}
              placeholder="科目名，如「高等数学」"
              style={{ flex: 1, border: '1px solid var(--border-hi)', borderRadius: 6, padding: '5px 8px', fontSize: 12.5, background: 'var(--paper)' }}
              onChange={(e) => setSubjectName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && subjectName.trim()) {
                  void props.onCreateSubject(subjectName.trim()).then(() => setAddingSubject(false))
                }
                if (e.key === 'Escape') setAddingSubject(false)
              }}
            />
          </div>
        )}

        <DndContext
          sensors={query ? [] : sensors}
          measuring={{ droppable: { strategy: MeasuringStrategy.Always } }}
          collisionDetection={(args) => {
            // 显式落点（插入线/移入区/科目标题）用「拖拽 ghost 当前矩形」与各 droppable 的
            // 实测矩形相交判定——rectIntersection/pointerWithin 依赖的 active rect 在拖动中
            // 冻结于源位置，远距离拖动永远够不到目标，这是官方 collision 函数在此场景失效的根因。
            const ghost = args.collisionRect
            if (ghost && ghost.height > 0) {
              const hits: { id: string | number; data?: unknown; dist: number }[] = []
              for (const c of args.droppableContainers) {
                const id = String(c.id)
                if (!id.startsWith('insert') && !id.startsWith('enter-folder') && !id.startsWith('to-subject-root')) continue
                if (c.disabled) continue
                const ref = c.rect as unknown as { current?: ClientRect | null }
                const r = ref && typeof ref === 'object' && 'current' in ref ? ref.current : null
                if (!r || r.height <= 0) continue
                const overlaps = !(ghost.right < r.left || ghost.left > r.right || ghost.bottom < r.top || ghost.top > r.bottom)
                if (overlaps) {
                  const cy = (Math.max(ghost.top, r.top) + Math.min(ghost.bottom, r.bottom)) / 2
                  hits.push({ id: c.id, dist: Math.abs(cy - (r.top + r.bottom) / 2) })
                }
              }
              if (hits.length) return hits.sort((a, b) => a.dist - b.dist) as Collision[]
            }
            return closestCorners(args)
          }}
          onDragStart={handleDragStart}
          onDragOver={handleDragOver}
          onDragEnd={handleDragEnd}
          onDragCancel={handleDragCancel}
        >
          <SortableContext items={viewItems.subjects ?? []} strategy={verticalListSortingStrategy}>
            {visibleSubjects.map((s, idx) => {
              const subjectInsertId = `insert-subject:${s.id}:${idx}`
              const subjList = viewItems.subjects ?? []
              const activeSubjIdx = dragState && activeKind === 'subject' ? subjList.indexOf(dragState.activeId) : -1
              return (
                <div key={s.id}>
                  <InsertionDropZone
                    containerKey={`children:${s.id}:root`}
                    index={idx}
                    dropId={subjectInsertId}
                    enabled={activeKind === 'subject' && idx !== activeSubjIdx && idx !== activeSubjIdx + 1}
                    active={overTargetId === subjectInsertId}
                    rootEnd={false}
                  />
                  <SubjectBlock
                    subject={s}
                    viewItems={viewItems}
                    docMap={docMap}
                    folderMap={folderMap}
                    collapsed={collapsed[s.id] ?? false}
                    activeSubjectId={activeSubjectId}
                    activeDocId={activeDocId}
                    overTargetId={overTargetId}
                    activeKind={activeKind}
                    activeIsDoc={activeIsDoc}
                    activeFolderItem={activeKind === 'folder' ? dragState?.activeId ?? null : null}
                    addingFolderFor={addingFolderFor}
                    folderName={folderName}
                    setAddingFolderFor={setAddingFolderFor}
                    setFolderName={setFolderName}
                    onSelectSubject={props.onSelectSubject}
                    onToggleSubject={props.onToggleSubject}
                    onSelectDoc={props.onSelectDoc}
                    onCreateFolder={props.onCreateFolder}
                    query={query}
                    confirmDeleteId={confirmDeleteId}
                    deletingId={deletingId}
                    requestDelete={requestDelete}
                    executeDelete={executeDelete}
                    cancelDelete={cancelDelete}
                  />
                </div>
              )
            })}
          </SortableContext>

          <DragOverlay>
            {dragState && activeKind === 'subject' && (
              <div className="sb-drag-ghost">
                <span className="caret">▼</span>
                <span>{subjectMap.get(idOf(dragState.activeId))?.name ?? ''}</span>
              </div>
            )}
            {dragState && activeKind === 'folder' && (
              <div className="sb-drag-ghost">
                {FolderIcon}
                <span>{folderMap.get(idOf(dragState.activeId))?.name ?? ''}</span>
              </div>
            )}
            {dragState && activeKind === 'doc' && (
              <div className="sb-drag-ghost">
                {FileIcon}
                <span>{docMap.get(idOf(dragState.activeId))?.filename.replace(/\.pdf$/i, '') ?? ''}</span>
              </div>
            )}
          </DragOverlay>
        </DndContext>

        {visibleSubjects.length === 0 && (
          <div style={{ padding: '18px 10px', fontSize: 12, color: 'var(--ink-3)', textAlign: 'center' }}>没有匹配的科目或课件</div>
        )}
      </div>

      <div className="sb-actions-foot">
        <button className="sb-foot subtle" title="设置 / 模型与 API" onClick={props.onOpenModelSettings}>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" style={{ color: 'var(--ink-3)' }}>
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 .6 1.7 1.7 0 0 0-.4 1.1V21a2 2 0 1 1-4 0v-.09A1.7 1.7 0 0 0 8.6 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.1-1H3a2 2 0 1 1 0-4h.09A1.7 1.7 0 0 0 4.6 8.6a1.7 1.7 0 0 0-.34-1.88l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.7 1.7 0 0 0 9 4.6h.1A1.7 1.7 0 0 0 10 3.1V3a2 2 0 1 1 4 0v.09A1.7 1.7 0 0 0 15 4.6a1.7 1.7 0 0 0 1.88-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.7 1.7 0 0 0 19.4 9v.1A1.7 1.7 0 0 0 21 10h.09a2 2 0 1 1 0 4H21a1.7 1.7 0 0 0-1.6 1z" />
          </svg>
          <span className="ftxt">设置</span>
          <span className="tag">模型与 API</span>
        </button>
        <button className="sb-foot" title="上传课件（PDF / PPT / Word）" onClick={() => fileRef.current?.click()}>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" style={{ color: 'var(--ink-3)' }}>
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
            <polyline points="17 8 12 3 7 8" />
            <line x1="12" y1="3" x2="12" y2="15" />
          </svg>
          <span className="ftxt">上传课件</span>
          <span className="tag">PDF · PPT · Word</span>
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".pdf,.pptx,.ppt,.docx,.doc"
          style={{ display: 'none' }}
          onChange={(e) => {
            const f = e.target.files?.[0]
            if (f) props.onUpload(f)
            e.target.value = ''
          }}
        />
      </div>

      {/* 跨科目移动确认（课件 / 文件夹通用） */}
      {pendingMove && (
        <div className="sb-confirm-mask" onClick={cancelPendingMove}>
          <div className="sb-confirm" onClick={(e) => e.stopPropagation()}>
            <div className="t">将{pendingMove.kind === 'folder' ? '文件夹（含内容）' : '课件'}移到科目「{subjectMap.get(pendingMove.toSid)?.name ?? ''}」？</div>
            <div className="d">{pendingMove.name}</div>
            <div className="btns">
              <button className="cancel" onClick={cancelPendingMove}>取消</button>
              <button className="ok" onClick={confirmCrossMove}>移入</button>
            </div>
          </div>
        </div>
      )}
    </aside>
  )
}
