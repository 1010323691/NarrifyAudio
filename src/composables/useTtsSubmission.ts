import { computed, onMounted, onUnmounted, onActivated, onDeactivated, ref, watch } from 'vue'
import { ApiError } from '@/api/client'
import { getSubmission, runBatch, submitBatchReset, type BatchTaskSubmission } from '@/api/tts'
import { getDurableTask } from '@/api/durableTasks'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'
import { randomUuid } from '@/utils/uuid'

type Phase = 'submitting' | 'confirming' | 'resetting' | 'queued'
interface Operation {
  key: string
  scope: string
  projectId: string
  names: string[]
  mode: 'resume' | 'reset'
  phase: Phase
  resetDone: boolean
  resetTaskId?: string
  taskIds: string[]
  started: number
}
const prefix = 'narrify:tts-submission:'
const operations = ref<Record<string, Operation>>({})
const executing = new Set<string>()
function read(scope: string) {
  try {
    const raw = localStorage.getItem(prefix + scope)
    const value = raw ? JSON.parse(raw) : null
    if (value?.scope === scope && typeof value.key === 'string' && Array.isArray(value.names) && value.names.length <= 500) return value as Operation
  } catch { /* Browser storage may be unavailable. Server fencing still applies. */ }
  return null
}
function save(scope: string, op: Operation | null) {
  const next = { ...operations.value }
  if (op) next[scope] = op
  else delete next[scope]
  operations.value = next
  try {
    if (op) {
      const encoded = JSON.stringify(op)
      if (localStorage.getItem(prefix + scope) !== encoded) localStorage.setItem(prefix + scope, encoded)
    }
    else localStorage.removeItem(prefix + scope)
  } catch { /* Keep the in-memory lock. */ }
}

/** An uncertain HTTP result never grants permission to create a new operation. */
export function useTtsSubmission() {
  const auth = useAuthStore()
  const project = useProjectStore()
  const scope = computed(() => `${auth.user?.id ?? ''}:${project.activeProjectId ?? ''}`)
  const operation = computed(() => operations.value[scope.value] ?? null)
  const receipt = ref<BatchTaskSubmission | null>(null)
  const failure = ref('')
  const now = ref(Date.now())
  let timer: ReturnType<typeof setTimeout> | null = null
  let feedbackTimer: ReturnType<typeof setTimeout> | null = null
  let version = 0
  let attempts = 0
  let alive = true
  let active = true
  const locked = computed(() => !!operation.value)
  const label = computed(() => {
    const op = operation.value
    if (!op) return ''
    if (op.phase === 'confirming') return '正在确认提交结果…'
    if (op.phase === 'resetting') return '正在重置所选音频…'
    if (op.phase === 'queued') return '已提交，等待任务执行'
    return now.value - op.started >= 5000 ? '服务器正在处理提交，请稍候…' : '正在提交合成任务…'
  })
  function schedule() {
    if (!alive || !active || timer || !operation.value) return
    const delay = Math.min(10000, 1000 * 2 ** Math.min(attempts++, 4))
    timer = setTimeout(() => { timer = null; now.value = Date.now(); void recover() }, delay)
  }
  async function perform(op: Operation): Promise<BatchTaskSubmission | null> {
    if (executing.has(op.scope)) return null
    executing.add(op.scope)
    const epoch = version
    const current = () => alive && epoch === version && scope.value === op.scope
    try {
      if (op.mode === 'reset' && !op.resetDone) {
        if (!op.resetTaskId) {
          save(op.scope, { ...op, phase: 'submitting' })
          const reset = await submitBatchReset(op.names, op.key + ':reset', op.projectId)
          op = { ...op, resetTaskId: reset.task_id, phase: 'resetting' }
          save(op.scope, op)
        }
        const resetTask = await getDurableTask(op.resetTaskId!)
        if (!['succeeded', 'failed', 'cancelled', 'timeout'].includes(resetTask.status)) return null
        if (resetTask.status !== 'succeeded') {
          save(op.scope, null)
          if (current()) failure.value = resetTask.error_message || '重置合成包失败'
          return null
        }
        op = { ...op, resetDone: true, phase: 'submitting' }
        save(op.scope, op)
      }
      if (!current()) return null
      const submitted = await runBatch({ scripts: op.names }, op.key + ':batch', op.projectId)
      save(op.scope, { ...op, taskIds: submitted.task_ids, phase: 'queued' })
      if (current()) receipt.value = submitted
      return submitted
    } catch (error) {
      // Only a definitive client-side rejection can release this operation.
      if (error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408) {
        save(op.scope, null)
        if (current()) failure.value = error.message
      } else {
        save(op.scope, { ...op, phase: op.resetTaskId && !op.resetDone ? 'resetting' : 'confirming' })
      }
      return null
    } finally {
      executing.delete(op.scope)
      if (current()) schedule()
    }
  }
  async function recover() {
    const op = operation.value
    if (!active) return
    if (!op || document.hidden || executing.has(op.scope)) { schedule(); return }
    const epoch = version
    const key = op.key + (op.mode === 'reset' && !op.resetDone ? ':reset' : ':batch')
    try {
      const found = await getSubmission(key)
      if (epoch !== version || scope.value !== op.scope) return
      if (found.project_id !== op.projectId) throw new Error('提交项目不一致')
      if (op.mode === 'reset' && !op.resetDone) {
        const next = { ...op, resetTaskId: found.task_ids[0], phase: 'resetting' as const }
        save(op.scope, next)
        await perform(next)
      } else {
        const statuses = Object.values(found.statuses)
        if (statuses.length === found.task_ids.length && statuses.every(s => ['succeeded', 'failed', 'cancelled', 'timeout'].includes(s))) {
          receipt.value = found
          save(op.scope, null)
        } else {
          save(op.scope, { ...op, taskIds: found.task_ids, phase: 'queued' })
          if (!receipt.value || receipt.value.task_ids.join('|') !== found.task_ids.join('|')) receipt.value = found
        }
      }
    } catch (error) {
      if (epoch !== version || scope.value !== op.scope) return
      if (error instanceof ApiError && error.status === 404 && !op.taskIds.length) await perform(op)
    } finally { if (epoch === version) schedule() }
  }
  async function submit(names: string[], mode: 'resume' | 'reset' = 'resume') {
    const saved = read(scope.value)
    if (saved) save(scope.value, saved)
    if (locked.value || !auth.user?.id || !project.activeProjectId) return null
    failure.value = ''; receipt.value = null; attempts = 0
    const op: Operation = { key: randomUuid(), scope: scope.value, projectId: project.activeProjectId,
      names: [...names], mode, phase: 'submitting', resetDone: mode === 'resume', taskIds: [], started: Date.now() }
    save(op.scope, op)
    if (feedbackTimer) clearTimeout(feedbackTimer)
    feedbackTimer = setTimeout(() => { now.value = Date.now(); feedbackTimer = null }, 5000)
    schedule()
    return perform(op)
  }
  function restore() {
    version++; attempts = 0; failure.value = ''; receipt.value = null
    if (timer) clearTimeout(timer)
    timer = null
    const saved = read(scope.value)
    if (saved) save(scope.value, saved)
    if (operation.value) { void recover(); schedule() }
  }
  // Updating memory from a storage event must not re-write storage (which loops across tabs).
  function onStorage(event: StorageEvent) {
    if (event.key !== prefix + scope.value) return
    const next = { ...operations.value }; const saved = read(scope.value)
    if (saved) next[scope.value] = saved
    else delete next[scope.value]
    operations.value = next
    schedule()
  }
  watch(scope, restore, { flush: 'sync', immediate: true })
  onMounted(() => window.addEventListener('storage', onStorage))
  onDeactivated(() => { active = false; if (timer) clearTimeout(timer); timer = null })
  onActivated(() => { active = true; if (operation.value) { void recover(); schedule() } })
  onUnmounted(() => { alive = false; version++; if (timer) clearTimeout(timer); if (feedbackTimer) clearTimeout(feedbackTimer); window.removeEventListener('storage', onStorage) })
  function settled(taskIds: string[]) {
    const op = operation.value
    if (op?.phase === 'queued' && op.taskIds.length === taskIds.length && op.taskIds.every(id => taskIds.includes(id))) save(op.scope, null)
  }
  return { locked, label, receipt, failure, submit, settled }
}
