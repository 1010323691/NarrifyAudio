<script setup lang="ts">
import { computed } from 'vue'
import { LoaderCircle, Pause, ArrowUpRight } from 'lucide-vue-next'
import { taskProgressLabel } from '@/utils/resources'
import type { TaskSnapshot } from '@/types'

const props = withDefaults(defineProps<{ tasks: TaskSnapshot[]; detailed?: boolean; limit?: number }>(), { limit: 2 })
const summary = computed(() => {
  const running = props.tasks.filter(item => ['running', 'cancelling'].includes(item.status)).length
  const waiting = props.tasks.filter(item => ['pending', 'queued', 'retrying'].includes(item.status)).length
  const paused = props.tasks.filter(item => item.status === 'paused').length
  return [running && `${running}个运行中`, waiting && `${waiting}个排队`, paused && `${paused}个暂停`].filter(Boolean).join(' · ')
})
</script>

<template>
  <div v-if="tasks.length" class="rc-task-brief" :class="{ 'is-detailed': detailed }">
    <div class="rc-task-summary"><LoaderCircle v-if="tasks.some(item => item.status === 'running')" class="rc-spin" :size="13" /><Pause v-else :size="13" /><span>{{ summary }}</span></div>
    <div v-for="item in tasks.slice(0, limit)" :key="item.id" class="rc-task-line">
      <div class="rc-task-label"><span :title="item.label">{{ item.label }}</span><strong>{{ taskProgressLabel(item) }}</strong></div>
      <div v-if="item.status === 'running' && item.progress > 0" class="rc-progress" role="progressbar" :aria-label="`${item.label}进度`" :aria-valuenow="Math.round(item.progress * 100)" aria-valuemin="0" aria-valuemax="100"><span :style="{ width: `${Math.min(1, item.progress) * 100}%` }" /></div>
      <p v-if="detailed && (item.current || item.phase)" class="rc-task-phase">{{ item.current || item.phase }}</p>
    </div>
    <RouterLink v-if="detailed || tasks.length > limit" class="rc-text-link rc-task-link" to="/tasks">查看全部任务<ArrowUpRight :size="12" /></RouterLink>
  </div>
</template>
