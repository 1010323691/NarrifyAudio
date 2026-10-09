<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { discardRejectedSubmission, pendingSubmissions, restoreSubmissions, resumeSubmission, submittingBatches } from '@/api/batchSubmission'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'
import { useTaskStore } from '@/stores/task'
import Button from '@/components/ui/Button.vue'

const props = defineProps<{ routes: string[] }>()
const emit = defineEmits<{ committed: [] }>()
const auth = useAuthStore(), project = useProjectStore(), tasks = useTaskStore()
const scope = computed(() => `${auth.user?.id ?? ''}:${project.activeProjectId}`)
const error = ref('')
const pending = computed(() => Object.values(pendingSubmissions.value).filter(op => op.scope === scope.value && props.routes.includes(op.route)))
watch([scope, () => props.routes], () => {
  error.value = ''
  if (!auth.user?.id || !project.activeProjectId) return
  try { restoreSubmissions(scope.value, props.routes) } catch (cause) { error.value = (cause as Error).message }
}, { immediate: true })
const busy = (route: string) => submittingBatches.value.includes(`${scope.value}:${route}`)
const title = (route: string) => ({ '/api/tts/prepare-foundations': '角色基础', '/api/tts/make-clones': '角色克隆',
  '/api/bgm/match': 'BGM 匹配', '/api/bgm/analyze-segment': '段落分析', '/api/bgm/mix': 'BGM 混音' } as Record<string, string>)[route] || '文本解析'
async function resume(route: string) {
  const selectedScope = scope.value
  error.value = ''
  try {
    await resumeSubmission(selectedScope, route)
    if (scope.value !== selectedScope) return
    await tasks.refresh()
    if (scope.value === selectedScope) emit('committed')
  } catch (cause) {
    if (scope.value === selectedScope && (cause as Error).name !== 'AbortError') error.value = (cause as Error).message
  }
}
function discard(route: string) {
  try { discardRejectedSubmission(scope.value, route); error.value = '' }
  catch (cause) { error.value = (cause as Error).message }
}
</script>

<template>
  <div v-if="pending.length || error" class="space-y-2 rounded-md border border-primary/30 bg-primary/5 p-3 text-sm" role="status">
    <div v-for="op in pending" :key="op.route" class="flex flex-wrap items-center gap-2">
      <span>{{ title(op.route) }}：已确认提交 {{ op.offset }}/{{ op.items.length }} 项。</span>
      <Button size="sm" variant="outline" :disabled="busy(op.route)" @click="resume(op.route)">{{ busy(op.route) ? '正在确认提交…' : '继续提交' }}</Button>
      <Button v-if="op.rejected" size="sm" variant="ghost" :disabled="busy(op.route)" @click="discard(op.route)">清除被拒绝的未提交部分</Button>
      <span v-if="op.rejected" class="text-muted-foreground">已提交任务保留在任务中心。</span>
    </div>
    <p v-if="error" class="text-destructive">{{ error }}</p>
  </div>
</template>
