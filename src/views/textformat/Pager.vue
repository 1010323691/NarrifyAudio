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

/** 页码窗口：固定 7 个页码位（含「…」占位），分页条宽度恒定，切页只换中间内容。
    首末页常驻；中间窗口随当前页滑动（cur±1），靠边时吸附到边并展成 5 页。
    例（17 页）：
      第 1 页： [1]  2  3  4  5  …  17
      第 3 页：  1  2  [3]  4  5  …  17
      第 5 页：  1  …  4  [5]  6  …  17
      第15页：   1  … 13 14 [15] 16  17 */
const pageItems = computed<(number | '...')[]>(() => {
  const n = props.pageCount
  if (n <= 7) return Array.from({ length: n }, (_, i) => i + 1)
  const cur = props.page
  // 靠边窗口：≤3 吸附首页展到 5；≥n-2 吸附末页展到 n-4；中段固定 3（cur±1）。
  const win: [number, number] =
    cur <= 3 ? [1, 5] : cur >= n - 2 ? [n - 4, n] : [cur - 1, cur + 1]
  const start = Math.max(1, win[0])
  const end = Math.min(n, win[1])
  const items: (number | '...')[] = []
  if (start > 1) {
    items.push(1)
    if (start > 2) items.push('...')
  }
  for (let i = start; i <= end; i += 1) items.push(i)
  if (end < n) {
    if (end < n - 1) items.push('...')
    items.push(n)
  }
  return items
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
