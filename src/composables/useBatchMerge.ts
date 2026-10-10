// 批量合并疑似角色：弹窗的状态与动作。
// 图数据来自后端指向关系表（merge-graph），所有写操作带 graph.version（乐观锁），成功后整图替换。
// 选中/展开是纯前端状态，不持久化；「新增」「待复核」由后端持久化，离开目标角色时才标记已查看。
import { computed, ref } from 'vue'
import { getMergeGraph, markMergeReviewed, mergeBatch, restoreLink, undoMerge, vetoLink } from '@/api/tts'
import type { MergeBatchResult, MergeGraph, MergeRole } from '@/types'
import {
  badgeCount, defaultExpanded, emptyRole, filterTargets, listTargets, mergePlan, pruneSelection, visibleRows,
} from './batchMergeGraph'

const REFRESH_THROTTLE_MS = 5000
const CONFLICT = '角色数据已变化，已刷新，请重新确认'

export function useBatchMerge(options: { script: string; captureScope: () => () => boolean }) {
  const { script, captureScope } = options
  const graph = ref<MergeGraph | null>(null)
  const open = ref(false)
  const loading = ref(false)
  const busy = ref(false)
  const error = ref('')
  const currentTarget = ref<string | null>(null)
  const selected = ref<Set<string>>(new Set())
  const expanded = ref<Set<string>>(new Set())
  const sessionTargets = ref<Set<string>>(new Set())
  const query = ref('')
  const onlyPending = ref(false)
  const alertDismissed = ref(false)
  const roleCache = new Map<string, MergeRole>()
  let generation = 0
  let lastRefresh = 0

  const roleOf = (name: string) => roleCache.get(name) ?? emptyRole(name)
  const targets = computed(() => (graph.value ? listTargets(graph.value, sessionTargets.value) : []))
  const visibleTargets = computed(() => filterTargets(targets.value, query.value, onlyPending.value))
  const badge = computed(() => badgeCount(targets.value))
  /** Button is shown only while the scope has any link or any merge record. */
  const hasAny = computed(() => !!graph.value && (Object.keys(graph.value.links).length > 0 || graph.value.records.length > 0))
  const rows = computed(() => (graph.value && currentTarget.value
    ? visibleRows(graph.value, roleOf, currentTarget.value, expanded.value, selected.value) : []))
  const records = computed(() => (graph.value ?? { records: [] }).records.filter(r => r.target === currentTarget.value))
  const vetoes = computed(() => (graph.value?.vetoes ?? []).filter(v => v.target === currentTarget.value))
  const newCount = computed(() => (currentTarget.value && graph.value ? graph.value.new_candidates[currentTarget.value]?.length ?? 0 : 0))
  const plan = computed(() => (graph.value && currentTarget.value
    ? mergePlan(graph.value, roleOf, currentTarget.value, selected.value) : null))

  function setGraph(next: MergeGraph) {
    // A late reply must never roll the view back to an older snapshot of the same scope.
    if (graph.value && next.version < graph.value.version) return
    for (const role of next.roles) roleCache.set(role.name, role)
    graph.value = next
    if (open.value) {
      const shown = new Set(sessionTargets.value)
      for (const entry of listTargets(next, shown)) shown.add(entry.name)
      sessionTargets.value = shown
      const gone = currentTarget.value && !listTargets(next, shown).some(e => e.name === currentTarget.value)
      if (gone) currentTarget.value = null
    }
    selected.value = pruneSelection(next, currentTarget.value, selected.value)
  }

  /** Reload the graph for the toolbar badge. Cheap callers (list reloads) are throttled; `force` skips that. */
  async function refresh(force = false) {
    if (!force && Date.now() - lastRefresh < REFRESH_THROTTLE_MS) return
    lastRefresh = Date.now()
    const isCurrent = captureScope()
    const mine = generation
    try {
      const next = await getMergeGraph(script)
      if (isCurrent() && mine === generation) setGraph(next)
    } catch { /* badge only: the dialog reports its own load errors */ }
  }

  async function openDialog() {
    const isCurrent = captureScope()
    const mine = ++generation
    open.value = true
    loading.value = true
    error.value = ''
    currentTarget.value = null
    selected.value = new Set()
    sessionTargets.value = new Set()
    try {
      const next = await getMergeGraph(script)
      if (isCurrent() && mine === generation) setGraph(next)
    } catch (e: any) {
      if (isCurrent() && mine === generation) error.value = e?.message || '加载失败，请重试。'
    } finally {
      if (mine === generation) loading.value = false
    }
  }

  /** Leaving a target counts as having reviewed its new candidates. */
  function leaveCurrent() {
    const target = currentTarget.value
    const current = graph.value
    if (!target || !current || !current.new_candidates[target]?.length) return
    const { [target]: _seen, ...rest } = current.new_candidates
    graph.value = { ...current, new_candidates: rest }
    void markMergeReviewed(script, target).catch(() => {})
  }

  function closeDialog() {
    leaveCurrent()
    ++generation
    open.value = false
    loading.value = false
    busy.value = false
    error.value = ''
    currentTarget.value = null
    selected.value = new Set()
    expanded.value = new Set()
    sessionTargets.value = new Set()
    query.value = ''
    onlyPending.value = false
    alertDismissed.value = false
  }

  /** Account/project switched: drop everything, including in-flight responses. */
  function reset() {
    closeDialog()
    graph.value = null
    roleCache.clear()
    lastRefresh = 0
  }

  function selectTarget(name: string) {
    if (!graph.value || name === currentTarget.value) return
    leaveCurrent()
    currentTarget.value = name
    selected.value = new Set()
    expanded.value = defaultExpanded(graph.value, name)
    alertDismissed.value = false
    error.value = ''
  }

  /** Clicking a row's name toggles selection; selecting also opens a collapsed branch. */
  function toggleSelect(name: string) {
    const next = new Set(selected.value)
    if (next.has(name)) next.delete(name)
    else {
      next.add(name)
      const row = rows.value.find(r => r.name === name)
      if (row?.hasChildren && !row.expanded) expanded.value = new Set(expanded.value).add(name)
    }
    selected.value = next
  }

  function toggleExpand(name: string) {
    const next = new Set(expanded.value)
    if (next.has(name)) next.delete(name)
    else next.add(name)
    expanded.value = next
  }

  const clearSelection = () => { selected.value = new Set() }

  /** One guarded write: serialises on `busy`, drops stale replies, refetches on version conflicts. */
  async function write<T extends { graph: MergeGraph }>(call: (version: number) => Promise<T>): Promise<T | null> {
    if (busy.value || !graph.value) return null
    const isCurrent = captureScope()
    const mine = generation
    busy.value = true
    error.value = ''
    try {
      const result = await call(graph.value.version)
      if (!isCurrent() || mine !== generation) return null
      setGraph(result.graph)
      return result
    } catch (e: any) {
      if (!isCurrent() || mine !== generation || e?.name === 'AbortError') return null
      if (e?.status === 409 && /角色数据已变化/.test(e?.message ?? '')) {
        error.value = CONFLICT
        try { setGraph(await getMergeGraph(script)) } catch { /* keep the old graph */ }
      } else error.value = e?.message || '操作失败'
      return null
    } finally {
      if (mine === generation) busy.value = false
    }
  }

  async function merge(): Promise<MergeBatchResult | null> {
    const target = currentTarget.value
    const sources = [...selected.value]
    if (!target || !sources.length) return null
    const result = await write(version => mergeBatch(script, target, sources, version))
    if (result) selected.value = new Set()
    return result
  }
  const undo = (recordId: string) => write(version => undoMerge(script, recordId, version))
  const veto = (source: string, target: string) => write(version => vetoLink(script, source, target, version))
  const restore = (source: string, target: string) => write(version => restoreLink(script, source, target, version))

  return {
    graph, open, loading, busy, error, currentTarget, selected, expanded, query, onlyPending, alertDismissed,
    targets, visibleTargets, badge, hasAny, rows, records, vetoes, newCount, plan, roleOf,
    refresh, openDialog, closeDialog, reset, selectTarget, toggleSelect, toggleExpand, clearSelection,
    merge, undo, veto, restore,
  } as const
}
