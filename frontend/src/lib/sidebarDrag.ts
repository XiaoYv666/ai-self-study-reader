export type SidebarItems = Record<string, string[]>

const isChildContainer = (containerKey: string) => containerKey.startsWith('children:')

export function containerOfIn(items: SidebarItems, itemId: string): string | null {
  for (const [containerKey, list] of Object.entries(items)) {
    if (list.includes(itemId)) return containerKey
  }
  return null
}

/**
 * Move an item using one immutable snapshot. The active item is removed from
 * every child container first, so stale drag-over frames cannot duplicate it.
 */
export function moveItemAcrossContainers(
  items: SidebarItems,
  activeId: string,
  targetContainer: string,
  index: number,
): SidebarItems {
  if (!isChildContainer(targetContainer) || !items[targetContainer]) return items

  const next: SidebarItems = {}
  for (const [containerKey, list] of Object.entries(items)) {
    next[containerKey] = isChildContainer(containerKey)
      ? list.filter((itemId) => itemId !== activeId)
      : [...list]
  }

  const target = [...next[targetContainer]]
  const insertAt = Math.max(0, Math.min(index, target.length))
  target.splice(insertAt, 0, activeId)
  next[targetContainer] = target
  return next
}

export interface InsertDropTarget {
  type: 'insert'
  containerKey: string
  index: number
}

export interface EnterFolderDropTarget {
  type: 'enter-folder'
  folderId: string
}

export type SidebarDropTarget = InsertDropTarget | EnterFolderDropTarget

export function parseSidebarDropTarget(dropId: string): SidebarDropTarget | null {
  if (dropId.startsWith('insert:')) {
    const rest = dropId.slice('insert:'.length)
    const separator = rest.lastIndexOf(':')
    if (separator < 0) return null
    const containerKey = rest.slice(0, separator)
    const index = Number(rest.slice(separator + 1))
    if (!isChildContainer(containerKey) || !Number.isInteger(index) || index < 0) return null
    return { type: 'insert', containerKey, index }
  }
  if (dropId.startsWith('enter-folder:')) {
    const folderId = dropId.slice('enter-folder:'.length)
    return folderId ? { type: 'enter-folder', folderId } : null
  }
  return null
}

/**
 * Compute the nesting depth of an item in the current snapshot.
 * Root-level items are depth 1; their children are depth 2 (the UI caps at 2).
 */
export function itemDepthIn(items: SidebarItems, itemId: string): number {
  // 深度从容器推导：根级容器（parentId 为 null）内的项 = depth 1，
  // 每上溯一层父文件夹 +1。起始 depth 取 0，首轮根容器判定即 +1 到正确值。
  let current = itemId
  let depth = 0
  const seen = new Set<string>()
  while (true) {
    const owner = containerOfIn(items, current)
    if (!owner || !owner.startsWith('children:')) return Math.max(depth, 1)
    const parts = owner.split(':') // children:<subjectId>:<parentId | undefined>
    const parentId = parts.slice(2).join(':') || null
    if (parentId === null) return depth + 1
    if (seen.has(parentId)) return Math.max(depth + 1, 1) // cycle guard
    seen.add(parentId)
    current = `folder:${parentId}`
    depth += 1
  }
}

/** Height of the subtree rooted at itemId (itself + all nested folders), ≥1. */
export function subtreeHeightIn(items: SidebarItems, itemId: string): number {
  // Walk every folder item and count how many sit inside itemId's subtree.
  const ownerByItem = new Map<string, string>()
  for (const [containerKey, list] of Object.entries(items)) {
    if (!isChildContainer(containerKey)) continue
    for (const id of list) ownerByItem.set(id, containerKey)
  }
  const folderIds: string[] = [itemId]
  let height = 1
  let grown = true
  while (grown) {
    grown = false
    for (const [id, owner] of ownerByItem.entries()) {
      if (folderIds.includes(id)) continue
      if (!id.startsWith('folder:')) continue
      const parts = owner.split(':')
      const parentId = parts.slice(2).join(':') || null
      if (parentId !== null && folderIds.includes(`folder:${parentId}`)) {
        folderIds.push(id)
        height += 1
        grown = true
      }
    }
  }
  return height
}

/** Apply an explicit insertion-line or folder-center drop using one snapshot. */
export function applySidebarDrop(items: SidebarItems, activeId: string, dropId: string): SidebarItems {
  const target = parseSidebarDropTarget(dropId)
  const sourceContainer = containerOfIn(items, activeId)
  if (!target || !sourceContainer) return items

  if (target.type === 'insert') {
    if (!items[target.containerKey]) return items
    // Folders may be inserted into the subject-root containers of ANY subject
    // (cross-subject moves land at target root and pop a confirm dialog),
    // but never into a nested container (depth would exceed 2; nested moves use enter-folder).
    if (activeId.startsWith('folder:')) {
      if (sourceContainer !== target.containerKey) {
        // 插入线跨容器仅允许落在科目根级容器（嵌套容器走 enter-folder 中心落点）；
        // 允许 depth-2 文件夹经此回到根级/跨科目（上限 2 层在根级自然满足）。
        if (parentOfContainerKey(target.containerKey) !== null) return items
      }
    }
    const sourceIndex = items[sourceContainer].indexOf(activeId)
    const index = sourceContainer === target.containerKey && sourceIndex >= 0 && sourceIndex < target.index
      ? target.index - 1
      : target.index
    return moveItemAcrossContainers(items, activeId, target.containerKey, index)
  }

  // Folder-center means "enter": docs always allowed; folders allowed only when
  // the resulting nesting stays within 2 levels (target is root-level and active subtree fits).
  const folderItemId = `folder:${target.folderId}`
  const folderOwner = containerOfIn(items, folderItemId)
  if (!folderOwner || !folderOwner.startsWith('children:')) return items
  const subjectId = folderOwner.split(':')[1]
  const targetContainer = `children:${subjectId}:${target.folderId}`
  if (!items[targetContainer]) return items
  if (!activeId.startsWith('doc:')) {
    if (!activeId.startsWith('folder:')) return items
    // 不能拖进自己（自己成为自己的子项 = 环）
    if (folderItemId === activeId) return items
    // 也不能拖进自己子树内的任何文件夹（环）
    if (isInSubtreeOf(items, activeId, folderItemId)) return items
    // 目标文件夹必须仍在科目根级（进入后 active 的深度 = 目标深度+1 ≤ 2）
    if (itemDepthIn(items, folderItemId) !== 1) return items
    // active 子树高度必须 ≤1（无子文件夹），否则嵌套超限；active 自身深度不限
    //（depth-2 文件夹平移到另一个根级文件夹下仍是 depth-2，合法）
    if (subtreeHeightIn(items, activeId) !== 1) return items
  }
  return moveItemAcrossContainers(items, activeId, targetContainer, items[targetContainer].length)
}

function parentOfContainerKey(containerKey: string): string | null {
  if (!isChildContainer(containerKey)) return null
  const parts = containerKey.split(':')
  const parentId = parts.slice(2).join(':') || null
  // 科目根级容器形如 children:<sid>:root —— 'root' 是占位符，不是真实 folderId
  return parentId === 'root' ? null : parentId
}

/** folderId 是否在 ancestorId 的子树内（含直接子级）。 */
function isInSubtreeOf(items: SidebarItems, ancestorId: string, folderId: string): boolean {
  if (ancestorId === folderId) return true
  const stack = [ancestorId]
  const seen = new Set<string>()
  while (stack.length) {
    const cur = stack.pop()!
    if (seen.has(cur)) continue
    seen.add(cur)
    for (const [ck, list] of Object.entries(items)) {
      if (!isChildContainer(ck)) continue
      const parentId = parentOfContainerKey(ck)
      if (parentId !== null && `folder:${parentId}` === cur) {
        for (const id of list) {
          if (id === folderId) return true
          if (id.startsWith('folder:')) stack.push(id)
        }
      }
    }
  }
  return false
}

export function validateUniqueChildItems(items: SidebarItems): void {
  const ownerByItem = new Map<string, string>()
  for (const [containerKey, list] of Object.entries(items)) {
    if (!isChildContainer(containerKey)) continue
    const withinContainer = new Set<string>()
    for (const itemId of list) {
      if (withinContainer.has(itemId)) {
        throw new Error(`拖拽状态无效：${itemId} 在 ${containerKey} 中重复`)
      }
      withinContainer.add(itemId)
      const previousOwner = ownerByItem.get(itemId)
      if (previousOwner) {
        throw new Error(`拖拽状态无效：${itemId} 同时出现在多个容器（${previousOwner}、${containerKey}）`)
      }
      ownerByItem.set(itemId, containerKey)
    }
  }
}
