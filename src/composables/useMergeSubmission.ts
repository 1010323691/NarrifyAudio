import { computed, onBeforeUnmount, onDeactivated, ref, watch } from 'vue'
import { ApiError } from '@/api/client'
import { runMerge, type MergeSubmissionReceipt } from '@/api/tts'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'
import { randomUuid } from '@/utils/uuid'

interface Operation {
  scope: string
  projectId: string
  names: string[]
  offset: number
  key: string
}
const prefix = 'narrify:merge-submission:'
const batchSize = 1000
const executing = new Set<string>()
const operations = ref<Record<string, Operation>>({})

/** Keep the uncertain batch's key; advance only after a durable receipt. */
export function useMergeSubmission(onCommitted: (receipt: MergeSubmissionReceipt) => void) {
  const auth = useAuthStore()
  const project = useProjectStore()
  const scope = computed(() => `${auth.user?.id ?? ''}:${project.activeProjectId ?? ''}`)
  const pending = computed(() => operations.value[scope.value] ?? null)
  const submitting = ref(false)
  const failure = ref('')
  const remaining = computed(() => pending.value ? pending.value.names.length - pending.value.offset : 0)
  const label = computed(() => pending.value
    ? `已提交 ${pending.value.offset}/${pending.value.names.length} 章${submitting.value ? '，继续提交…' : ''}` : '')
  let version = 0
  let alive = true
  function save(op: Operation | null, selectedScope: string) {
    const next = { ...operations.value }
    if (op) next[selectedScope] = op
    else delete next[selectedScope]
    operations.value = next
    try {
      if (op) localStorage.setItem(prefix + selectedScope, JSON.stringify(op))
      else localStorage.removeItem(prefix + selectedScope)
    } catch { /* In-memory retries still keep the original key. */ }
  }
  function restore() {
    version++
    submitting.value = false
    failure.value = ''
    try {
      const raw = localStorage.getItem(prefix + scope.value)
      const op = raw ? JSON.parse(raw) : null
      if (op?.scope === scope.value && op.projectId === project.activeProjectId &&
          Array.isArray(op.names) && op.names.length > 0 && op.names.length <= 10000 &&
          op.names.every((name: unknown) => typeof name === 'string') &&
          Number.isInteger(op.offset) && op.offset >= 0 && op.offset < op.names.length &&
          op.offset % batchSize === 0 && typeof op.key === 'string' && op.key.length > 0) operations.value = { ...operations.value, [scope.value]: op }
    } catch { /* Ignore malformed browser storage. */ }
  }
  watch(scope, restore, { immediate: true, flush: 'sync' })
  onDeactivated(() => { version++; submitting.value = false })
  onBeforeUnmount(() => { alive = false; version++; submitting.value = false })

  async function submit(names: string[] = []) {
    if (submitting.value || !auth.user?.id || !project.activeProjectId) return
    const selectedScope = scope.value
    if (executing.has(selectedScope)) return
    if (!pending.value) {
      const unique = [...new Set(names)]
      if (!unique.length) return
      save({ scope: selectedScope, projectId: project.activeProjectId,
        names: unique, offset: 0, key: randomUuid() }, selectedScope)
    }
    executing.add(selectedScope)
    submitting.value = true
    failure.value = ''
    const epoch = version
    const current = () => alive && version === epoch && scope.value === selectedScope
    let op = { ...pending.value }
    try {
      while (op.offset < op.names.length && current()) {
        const chunk = op.names.slice(op.offset, op.offset + batchSize)
        const receipt = await runMerge(chunk, op.key, op.projectId)
        if (!receipt || typeof receipt.batch_id !== 'string' || !Array.isArray(receipt.task_ids) ||
            receipt.task_ids.length !== chunk.length || !Array.isArray(receipt.packages) ||
            receipt.packages.length !== chunk.length || receipt.packages.some((row, index) =>
              row.package !== chunk[index] || row.task_id !== receipt.task_ids[index] || !row.task_id)) {
          throw new Error('提交回执不完整')
        }
        // Save confirmed progress even if the user left while this request ran.
        op = { ...op, offset: op.offset + chunk.length, key: randomUuid() }
        const next = op.offset < op.names.length ? op : null
        save(next, selectedScope)
        if (!current()) return
        // UI refresh errors must never turn a confirmed receipt into a failed submission.
        try { onCommitted(receipt) } catch { /* Receipt remains confirmed if a UI refresh fails. */ }
      }
    } catch (error) {
      if (!current()) return
      failure.value = error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408
        ? error.message : '提交结果尚未确认，请重试；会使用原提交标识核对结果。'
    } finally {
      executing.delete(selectedScope)
      if (current()) submitting.value = false
    }
  }
  return { submitting, failure, remaining, label, submit }
}
