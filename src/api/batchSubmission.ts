import { ref, watch } from 'vue'
import { ApiError, http } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'
import { randomUuid } from '@/utils/uuid'

type Receipt = { batch_id: string; task_ids: string[]; task_id?: string; files?: Record<string, unknown>[]; chapters?: Record<string, unknown>[] }
export interface PendingSubmission {
  version: 1
  scope: string
  projectId: string
  route: string
  field: string
  intent: string
  body: Record<string, unknown>
  items: unknown[]
  offset: number
  key: string
  receipts: Receipt[]
  error?: string
  rejected?: boolean
}
const prefix = 'narrify:task-batch:'
export const pendingSubmissions = ref<Record<string, PendingSubmission>>({})
export const submittingBatches = ref<string[]>([])
const executing = new Map<string, Promise<unknown>>()
const memory = new Map<string, PendingSubmission>()
const size = 1000
const identifier = (scope: string, route: string) => `${scope}:${route}`

function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().filter(k => (value as Record<string, unknown>)[k] !== undefined).map(k => `${JSON.stringify(k)}:${stable((value as Record<string, unknown>)[k])}`).join(',')}}`
  return JSON.stringify(value)
}

function save(id: string, op: PendingSubmission | null) {
  // Do not dispatch until the original key and payload survive a reload.
  try {
    if (op) localStorage.setItem(prefix + id, JSON.stringify(op))
    else localStorage.removeItem(prefix + id)
  } catch { throw new Error('无法保存提交进度，请恢复浏览器存储后重试。') }
  if (op) memory.set(id, op)
  else memory.delete(id)
  const next = { ...pendingSubmissions.value }
  if (op) next[id] = op
  else delete next[id]
  pendingSubmissions.value = next
}

function load(scope: string, route: string): PendingSubmission | null {
  const id = identifier(scope, route)
  if (memory.has(id)) return memory.get(id)!
  const raw = localStorage.getItem(prefix + id)
  if (!raw) return null
  const op = JSON.parse(raw) as PendingSubmission
  if (op.version !== 1 || op.scope !== scope || op.route !== route || op.projectId !== scope.split(':').at(-1) ||
      !op.body || typeof op.body !== 'object' || typeof op.field !== 'string' || typeof op.intent !== 'string' ||
      !Array.isArray(op.items) || !Array.isArray(op.receipts) || !Number.isInteger(op.offset) ||
      op.offset < 0 || op.offset > op.items.length || (op.offset !== op.items.length && op.offset % size !== 0) ||
      typeof op.key !== 'string' || !op.key || op.body.project_id !== op.projectId || op.intent !== stable(op.body) ||
      op.receipts.length !== Math.ceil(op.offset / size) || !op.receipts.every(r => typeof r.batch_id === 'string' && Array.isArray(r.task_ids))) {
    throw new Error('保存的提交进度无法读取，请检查浏览器存储。')
  }
  memory.set(id, op)
  pendingSubmissions.value = { ...pendingSubmissions.value, [id]: op }
  return op
}

export function restoreSubmissions(scope: string, routes: string[]) {
  for (const route of routes) load(scope, route)
}

function combined(receipts: Receipt[]) {
  const result = { ...receipts.at(-1), task_ids: receipts.flatMap(r => r.task_ids), batch_ids: receipts.map(r => r.batch_id) }
  for (const field of ['files', 'chapters'] as const) {
    if (receipts.some(r => Array.isArray(r[field]))) Object.assign(result, { [field]: receipts.flatMap(r => r[field] ?? []) })
  }
  delete result.task_id
  if (result.task_ids.length === 1) result.task_id = result.task_ids[0]
  return result
}

function checkReceipt(value: Receipt, chunk: unknown[], field: string) {
  if (!value || !value.batch_id || !Array.isArray(value.task_ids) || value.task_ids.length > chunk.length ||
      value.task_ids.some(id => typeof id !== 'string' || !id) || new Set(value.task_ids).size !== value.task_ids.length) throw new Error('提交回执不完整，请使用原提交标识重试。')
  const rows = field === 'files' ? value.files : value.chapters
  if (rows !== undefined && (!Array.isArray(rows) || rows.length !== value.task_ids.length ||
      rows.some((row, i) => row.task_id !== value.task_ids[i]))) throw new Error('提交回执不完整，请使用原提交标识重试。')
  if (field === 'files' && !rows) throw new Error('提交回执缺少章节明细，请使用原提交标识重试。')
}

async function perform<T>(op: PendingSubmission, id: string): Promise<T> {
  const auth = useAuthStore(), project = useProjectStore()
  let valid = true
  const stop = watch([() => auth.user?.id, () => project.activeProjectId], () => { valid = false }, { flush: 'sync' })
  const current = () => valid && `${auth.user?.id ?? ''}:${project.activeProjectId}` === op.scope
  submittingBatches.value = [...submittingBatches.value, id]
  try {
    while (op.offset < op.items.length) {
      if (!current()) throw new DOMException('工作台上下文已切换', 'AbortError')
      const chunk = op.items.slice(op.offset, op.offset + size)
      const receipt = await http.post<Receipt>(op.route, { ...op.body, [op.field]: chunk }, { headers: { 'Idempotency-Key': op.key } })
      checkReceipt(receipt, chunk, op.field)
      // A confirmed old-scope response advances only its own durable operation.
      op = { ...op, offset: op.offset + chunk.length, key: randomUuid(), receipts: [...op.receipts, receipt], error: undefined, rejected: false }
      save(id, op)
    }
    const result = combined(op.receipts)
    save(id, null)
    if (!current()) throw new DOMException('工作台上下文已切换', 'AbortError')
    return result as T
  } catch (error) {
    if ((error as Error)?.name !== 'AbortError' && memory.has(id)) {
      const definitive = error instanceof ApiError && [409, 422].includes(error.status) && !error.message.includes('幂等键')
      save(id, { ...memory.get(id)!, error: (error as Error)?.message || '提交结果尚未确认，请重试。', rejected: definitive })
    }
    throw error
  } finally {
    stop()
    submittingBatches.value = submittingBatches.value.filter(value => value !== id)
    executing.delete(id)
  }
}

export async function submitTaskBatches<T>(route: string, body: Record<string, unknown>, field: string,
    projectId?: string, resolveItems?: () => Promise<unknown[]>): Promise<T> {
  const auth = useAuthStore(), project = useProjectStore()
  const selected = projectId || project.activeProjectId
  const scope = `${auth.user?.id ?? ''}:${selected}`
  if (!auth.user?.id || !selected || selected !== project.activeProjectId) throw new DOMException('工作台上下文已切换', 'AbortError')
  const id = identifier(scope, route)
  const pinned = JSON.parse(JSON.stringify({ ...body, project_id: selected })) as Record<string, unknown>
  const intent = stable(pinned)
  let op = load(scope, route)
  if (op && op.intent !== intent) throw new Error('上次提交尚未确认，请先继续提交或清除已被拒绝的未提交部分。')
  if (executing.has(id)) return executing.get(id) as Promise<T>
  let valid = true
  const stop = watch([() => auth.user?.id, () => project.activeProjectId], () => { valid = false }, { flush: 'sync' })
  // Include selection resolution in the guard so duplicate clicks cannot race.
  const promise = (async () => {
    if (!op) {
      const items = Array.isArray(pinned[field]) ? pinned[field] as unknown[] : await resolveItems?.()
      if (!valid || `${auth.user?.id ?? ''}:${project.activeProjectId}` !== scope) throw new DOMException('工作台上下文已切换', 'AbortError')
      if (!items?.length) return { task_ids: [], files: [], chapters: [] } as T
      const unique = [...new Map(items.map(item => [stable(item), item])).values()]
      op = { version: 1, scope, projectId: selected, route, field, intent, body: pinned,
        items: unique, offset: 0, key: randomUuid(), receipts: [] }
      save(id, op)
    }
    return perform<T>(op, id)
  })()
  executing.set(id, promise)
  try { return await promise } finally { stop(); executing.delete(id) }
}

export async function resumeSubmission<T>(scope: string, route: string): Promise<T> {
  const op = load(scope, route)
  if (!op) throw new Error('没有待确认的提交。')
  return submitTaskBatches<T>(route, op.body, op.field, op.projectId)
}

export function discardRejectedSubmission(scope: string, route: string) {
  const id = identifier(scope, route), op = load(scope, route)
  if (!op?.rejected || executing.has(id)) throw new Error('提交结果尚未确认，请先继续提交。')
  save(id, null) // confirmed tasks remain in the task centre
}
