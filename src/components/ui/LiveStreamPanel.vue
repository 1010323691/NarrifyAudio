<script setup lang="ts">
/**
 * Presentational 「流式反馈」 panel for the 文本解析 page: renders the raw LLM stream
 * (``task.llm_stream``) as it arrives, in real time. It is a pure status display — it
 * reads only the Task snapshot's stream buffer and feeds no data into the parse / JSON
 * pipeline. Mirrors ``LiveLogPanel``'s shape (a titled box + an auto-following
 * ``ScrollArea``); the auto-follow reuses ``useLogAutoFollow`` on the stream string.
 */
import { computed } from 'vue'
import { useClientDisplayStore } from '@/stores/clientDisplay'
const clientDisplay = useClientDisplayStore()
import type { TaskSnapshot } from '@/types'
import { cn } from '@/lib/utils'
import { useLogAutoFollow } from '@/utils/logFollow'
import ScrollArea from '@/components/ui/ScrollArea.vue'

const props = defineProps<{
  task: TaskSnapshot | null
  /** Tailwind height class for the stream scroll area (default ``h-40``). */
  maxHeightClass?: string
}>()

const stream = computed(() => props.task?.llm_stream ?? '')

// Follow the stream to its bottom while the user is reading the tail (newest text last).
const { bind: bindStream } = useLogAutoFollow(() => stream.value)

const boxClass = computed(() => cn('rounded-md border bg-muted/40', props.maxHeightClass || 'h-40'))
</script>

<template>
  <div v-if="task && clientDisplay.logsEnabled" class="space-y-2.5">
    <div class="flex items-center justify-between gap-2">
      <span class="text-xs text-muted-foreground">模型输出</span>
    </div>

    <ScrollArea :ref="bindStream" :class="boxClass">
      <div class="whitespace-pre-wrap break-words p-3 font-mono text-xs leading-relaxed text-muted-foreground">
        {{ stream || '（等待 LLM 流式输出…）' }}
      </div>
    </ScrollArea>
  </div>
</template>
