<script setup lang="ts">
/**
 * Reusable real-time log panel for the long-running stage pages (角色配音 / 音频合成 /
 * 音频合并) and any other Task. It renders a Task's live progress + current step and its
 * log lines — newest last, level-coloured, timestamped — so a long job never looks
 * frozen. Purely presentational: the parent owns the Task and the cancel control
 * (drop one in the ``#actions`` slot).
 */
import { computed } from 'vue'
import type { TaskSnapshot } from '@/types'
import { cn } from '@/lib/utils'
import { formatClock } from '@/utils/format'
import { useLogAutoFollow } from '@/utils/log-follow'
import Progress from '@/components/ui/Progress.vue'
import ScrollArea from '@/components/ui/ScrollArea.vue'
import { Loader2 } from 'lucide-vue-next'

const props = defineProps<{
  task: TaskSnapshot | null
  /** Tailwind height class for the log scroll area (default ``h-72``). */
  maxHeightClass?: string
  /** Hide the progress bar / current-step row. */
  showProgress?: boolean
}>()

const logs = computed(() => props.task?.logs ?? [])

// While the task is in a live state the step row spins; once it lands in a terminal state
// (succeeded / failed / cancelled) the spinner stops so a finished run doesn't look busy.
const ACTIVE = ['pending', 'running', 'paused']
const isActive = computed(() => (props.task ? ACTIVE.includes(props.task.status) : false))

// Follow the log to its bottom while the user is reading the tail (newest line is last).
const { bind: bindLog } = useLogAutoFollow(() => logs.value)

const boxClass = computed(() => cn('rounded-md border bg-muted/40', props.maxHeightClass || 'h-72'))

// ERROR red, WARNING amber, everything else muted — matches the task center.
function logColor(level: string) {
  return level === 'ERROR'
    ? 'text-destructive'
    : level === 'WARNING'
      ? 'text-amber-500'
      : 'text-muted-foreground'
}
</script>

<template>
  <div v-if="task" class="space-y-2.5">
    <div v-if="showProgress !== false" class="space-y-1.5">
      <div class="flex items-center justify-between gap-3">
        <span class="flex min-w-0 items-center gap-2 text-sm">
          <Loader2 v-if="isActive" class="h-3.5 w-3.5 shrink-0 animate-spin text-primary" />
          <span class="truncate">{{ task.current || '处理中…' }}</span>
        </span>
        <span class="shrink-0 text-xs text-muted-foreground">{{ Math.round(task.progress * 100) }}%</span>
      </div>
      <Progress :value="task.progress" />
    </div>

    <div class="flex items-center justify-between gap-2">
      <span class="text-xs text-muted-foreground">实时日志</span>
      <slot name="actions" />
    </div>

    <ScrollArea :ref="bindLog" :class="boxClass">
      <div class="space-y-0.5 p-3 font-mono text-xs leading-relaxed">
        <div v-for="(l, i) in logs" :key="i" :class="logColor(l.level)">
          <span class="text-muted-foreground/60">{{ formatClock(l.t) }}</span>
          <span class="mx-1.5 font-semibold">{{ l.level }}</span>
          <span class="break-words">{{ l.msg }}</span>
        </div>
        <div v-if="!logs.length" class="text-muted-foreground/60">（暂无日志）</div>
      </div>
    </ScrollArea>
  </div>
</template>
