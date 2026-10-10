// 批量合并疑似角色：指向关系图的纯函数推导（无 Vue 依赖，便于沙箱回归）。
// 约定：links 是「角色 → 它疑似的目标」，每个角色最多一条；指向树 = 目标角色的全部（直接与间接）指向方。
import type { MergeGraph, MergeRole } from '@/types'

export type TargetTag = 'none' | 'merged' | 'review'
export interface TargetEntry { name: string; pending: number; tag: TargetTag; hasRecords: boolean }
export interface TreeRow {
  name: string
  depth: number // 0 = direct upstream of the target
  hasChildren: boolean
  expanded: boolean
  upstreamCount: number // all unmerged upstream roles (every level)
  isNew: boolean
  containsNew: boolean // collapsed hint: a new candidate is hidden below
  selectedBelow: number // collapsed hint: selected roles hidden below
  basis: string // rule behind this row's own link
  linkTarget: string // who this row points at
  targetSample: string // a sample line of linkTarget (evidence for the link)
  role: MergeRole
}
export interface PlanItem { name: string; lineCount: number; indirect: boolean }
export interface MergePlan {
  items: PlanItem[]
  lines: number
  voiceConfigs: number
  cloned: string[]
  orphans: string[] // roles that lose their target and will be rematched
}

const collator = new Intl.Collator('zh-Hans-CN')

export function emptyRole(name: string): MergeRole {
  return { name, gender: '', line_count: 0, sample: '', preview: '', voice_config: false, cloned: false }
}

export function childrenOf(graph: MergeGraph | null): Map<string, string[]> {
  const map = new Map<string, string[]>()
  for (const [source, link] of Object.entries(graph?.links ?? {})) {
    const list = map.get(link.target)
    if (list) list.push(source)
    else map.set(link.target, [source])
  }
  return map
}

/** Every role pointing at `node` directly or transitively (cycle-safe, never contains `node`). */
export function upstreamAll(children: Map<string, string[]>, node: string): Set<string> {
  const seen = new Set<string>()
  const stack = [...(children.get(node) ?? [])]
  while (stack.length) {
    const current = stack.pop()!
    if (seen.has(current) || current === node) continue
    seen.add(current)
    stack.push(...(children.get(current) ?? []))
  }
  return seen
}

/** Roles that are no longer independent: merged into something (top-level records and what they absorbed). */
export function mergedAway(graph: MergeGraph): Set<string> {
  const gone = new Set<string>()
  for (const record of graph.records) {
    gone.add(record.source)
    for (const child of record.children) gone.add(child)
  }
  return gone
}

export function listTargets(graph: MergeGraph, sessionTargets: Iterable<string>): TargetEntry[] {
  const children = childrenOf(graph)
  const gone = mergedAway(graph)
  const recordTargets = new Set(graph.records.map(r => r.target))
  const names = new Set<string>([...children.keys(), ...recordTargets, ...sessionTargets])
  const entries: TargetEntry[] = []
  for (const name of names) {
    if (gone.has(name)) continue
    const hasRecords = recordTargets.has(name)
    const review = hasRecords && (graph.new_candidates[name]?.length ?? 0) > 0
    entries.push({ name, pending: upstreamAll(children, name).size, hasRecords, tag: review ? 'review' : hasRecords ? 'merged' : 'none' })
  }
  return sortTargets(entries)
}

/** 待复核 first, then by pending count (desc, zero last), then by name (pinyin). */
export function sortTargets(entries: TargetEntry[]): TargetEntry[] {
  const rank = (e: TargetEntry) => (e.tag === 'review' ? 0 : e.pending > 0 ? 1 : 2)
  return [...entries].sort((a, b) => rank(a) - rank(b) || (rank(a) === 2 ? 0 : b.pending - a.pending) || collator.compare(a.name, b.name))
}

export function filterTargets(entries: TargetEntry[], query: string, onlyPending: boolean): TargetEntry[] {
  const q = query.trim().toLowerCase()
  return entries.filter(e => (!q || e.name.toLowerCase().includes(q)) && (!onlyPending || e.pending > 0))
}

export const badgeCount = (entries: TargetEntry[]) => entries.filter(e => e.pending > 0).length

/** Ancestors (below the target) of every new candidate: these branches open by default. */
export function defaultExpanded(graph: MergeGraph, target: string): Set<string> {
  const open = new Set<string>()
  for (const name of graph.new_candidates[target] ?? []) {
    const seen = new Set<string>([name])
    let parent = graph.links[name]?.target
    while (parent && parent !== target && !seen.has(parent)) {
      open.add(parent)
      seen.add(parent)
      parent = graph.links[parent]?.target
    }
  }
  return open
}

/** Roles below `target` that must no longer be selected after the graph changed. */
export function pruneSelection(graph: MergeGraph, target: string | null, selected: Set<string>): Set<string> {
  if (!target) return new Set()
  const valid = upstreamAll(childrenOf(graph), target)
  return new Set([...selected].filter(name => valid.has(name)))
}

export function visibleRows(graph: MergeGraph, roles: (name: string) => MergeRole, target: string,
  expanded: Set<string>, selected: Set<string>): TreeRow[] {
  const children = childrenOf(graph)
  const fresh = new Set(graph.new_candidates[target] ?? [])
  const hasNew = new Map<string, boolean>()
  const subtreeNew = (name: string, trail: Set<string>): boolean => {
    if (hasNew.has(name)) return hasNew.get(name)!
    const next = new Set(trail).add(name)
    const result = (children.get(name) ?? []).some(c => !next.has(c) && (fresh.has(c) || subtreeNew(c, next)))
    hasNew.set(name, result)
    return result
  }
  const rows: TreeRow[] = []
  const shown = new Set<string>([target])
  const upstreamCache = new Map<string, Set<string>>()
  const upstreamOf = (name: string) => {
    let cached = upstreamCache.get(name)
    if (!cached) upstreamCache.set(name, (cached = upstreamAll(children, name)))
    return cached
  }
  const walk = (parent: string, depth: number) => {
    const kids = (children.get(parent) ?? []).filter(k => !shown.has(k))
    kids.sort((a, b) => {
      const pa = fresh.has(a) || subtreeNew(a, new Set([target])) ? 0 : 1
      const pb = fresh.has(b) || subtreeNew(b, new Set([target])) ? 0 : 1
      return pa - pb || roles(b).line_count - roles(a).line_count || collator.compare(a, b)
    })
    for (const name of kids) {
      if (shown.has(name)) continue
      shown.add(name)
      const below = new Set(upstreamOf(name))
      below.delete(target)
      const open = expanded.has(name)
      const link = graph.links[name]
      rows.push({
        name, depth, role: roles(name), hasChildren: below.size > 0, expanded: open, upstreamCount: below.size,
        isNew: fresh.has(name), containsNew: !open && [...below].some(n => fresh.has(n)),
        selectedBelow: open ? 0 : [...below].filter(n => selected.has(n)).length,
        basis: link?.basis ?? '', linkTarget: link?.target ?? '', targetSample: link ? roles(link.target).sample : '',
      })
      if (open) walk(name, depth + 1)
    }
  }
  walk(target, 0)
  return rows
}

export function mergePlan(graph: MergeGraph, roles: (name: string) => MergeRole, target: string, selected: Set<string>): MergePlan {
  const items: PlanItem[] = []
  let lines = 0, voiceConfigs = 0
  const cloned: string[] = []
  for (const name of selected) {
    const role = roles(name)
    items.push({ name, lineCount: role.line_count, indirect: graph.links[name]?.target !== target })
    lines += role.line_count
    if (role.voice_config) voiceConfigs++
    if (role.cloned) cloned.push(name)
  }
  items.sort((a, b) => Number(a.indirect) - Number(b.indirect) || b.lineCount - a.lineCount || collator.compare(a.name, b.name))
  const orphans = Object.entries(graph.links)
    .filter(([source, link]) => selected.has(link.target) && !selected.has(source))
    .map(([source]) => source).sort(collator.compare)
  return { items, lines, voiceConfigs, cloned, orphans }
}
