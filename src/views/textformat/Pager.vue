<script setup lang="ts">
import { computed } from 'vue'
import { ChevronLeft, ChevronRight } from 'lucide-vue-next'
import { cn } from '@/lib/utils'

const props = defineProps<{
  page: number
  pageCount: number
  total: number
  pageSize: number
  pageSizeOptions?: number[]
  /** 左侧单位词，默认「章」。 */
  unit?: string
  class?: string
}>()
const emit = defineEmits<{
  (e: 'update:page', page: number): void
  (e: 'update:pageSize', size: number): void
}>()

const u = computed(() => props.unit ?? '章')

/** 左侧结果范围：「第 1–20 条，共 339 章」；空列表只给总数。 */
const rangeLabel = computed(() => {
  const t = props.total
  if (t === 0) return `共 0 ${u.value}`
  const from = (props.page - 1) * props.pageSize + 1
  const to = Math.min(props.page * props.pageSize, t)
  return `第 ${from}–${to} 条，共 ${t} ${u.value}`
})

/** 页码窗口：固定 5 个页码位（含「…」占位），分页条宽度恒定，切页只换中间内容。
    首末页常驻；n≤5 全显；靠边吸附展 4 页码 +「…」；中段 1 … c c+1 n。
    例（17 页）：
      第 1 页： [1]  2  3  4  …
      第 5 页：  1  …  [5]  6  17
      第15页：  … 14 15 16  [17] */
const pageItems = computed<(number | '...')[]>(() => {
  const n = props.pageCount
  if (n <= 5) return Array.from({ length: n }, (_, i) => i + 1)
  const cur = props.page
  if (cur <= 3) return [1, 2, 3, 4, '...']
  if (cur >= n - 2) return ['...', n - 3, n - 2, n - 1, n]
  return [1, '...', cur, cur + 1, n]
})
</script>

<template>
  <div :class="cn('flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-muted-foreground', props.class)">
    <span class="tabular-nums">{{ rangeLabel }}</span>
    <select
      class="h-7 cursor-pointer rounded-[8px] border border-input bg-background px-2 text-xs text-foreground"
      aria-label="每页条数"
      :value="props.pageSize"
      @change="emit('update:pageSize', Number(($event.target as HTMLSelectElement).value))"
    >
      <option v-for="n in props.pageSizeOptions ?? [20, 50]" :key="n" :value="n">{{ n }} 条 / 页</option>
    </select>
    <!-- 页码靠右：36×36 无边框点击区，当前页浅紫底紫字，其他页悬停浅灰底，键盘聚焦保留轮廓。 -->
    <nav class="ml-auto flex items-center gap-px" aria-label="分页">
      <button
        type="button"
        class="flex h-8 w-8 items-center justify-center rounded-[8px] text-foreground/70 transition-colors enabled:hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-40"
        aria-label="上一页"
        :disabled="props.page <= 1"
        @click="emit('update:page', props.page - 1)"
      >
        <ChevronLeft class="h-4 w-4" />
      </button>
      <template v-for="(item, i) in pageItems" :key="i">
        <span v-if="item === '...'" class="flex h-8 w-8 items-center justify-center" aria-hidden="true">…</span>
        <button
          v-else
          type="button"
          class="flex h-8 w-8 items-center justify-center rounded-[8px] tabular-nums transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          :class="item === props.page
            ? 'bg-primary/10 text-primary'
            : 'text-foreground/70 hover:bg-muted'"
          :aria-current="item === props.page ? 'page' : undefined"
          @click="emit('update:page', item)"
        >{{ item }}</button>
      </template>
      <button
        type="button"
        class="flex h-8 w-8 items-center justify-center rounded-[8px] text-foreground/70 transition-colors enabled:hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-40"
        aria-label="下一页"
        :disabled="props.page >= props.pageCount"
        @click="emit('update:page', props.page + 1)"
      >
        <ChevronRight class="h-4 w-4" />
      </button>
    </nav>
  </div>
</template>
