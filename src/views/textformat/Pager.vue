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

/** 页码窗口：≤7 页全显示；否则固定「首页 + 当前±1（靠边时 3 个）+ 末页」。
    例：17 页 → ‹ 1 2 3 … 17 ›，避免页码整排展开占用视觉空间。 */
const pageItems = computed<(number | '...')[]>(() => {
  const n = props.pageCount
  if (n <= 7) return Array.from({ length: n }, (_, i) => i + 1)
  const cur = props.page
  let start = Math.max(2, cur - 1)
  let end = Math.min(n - 1, cur + 1)
  if (cur <= 2) { start = 2; end = Math.min(3, n - 1) }
  else if (cur >= n - 1) { start = Math.max(2, n - 2); end = n - 1 }
  const items: (number | '...')[] = [1]
  if (start > 2) items.push('...')
  for (let i = start; i <= end; i += 1) items.push(i)
  if (end < n - 1) items.push('...')
  items.push(n)
  return items
})
</script>

<template>
  <div :class="cn('flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-muted-foreground', props.class)">
    <span class="tabular-nums">共 {{ props.total }} {{ u }}</span>
    <label class="flex items-center gap-1.5">
      每页
      <select
        class="h-7 rounded-md border bg-background px-1.5 text-xs"
        :value="props.pageSize"
        @change="emit('update:pageSize', Number(($event.target as HTMLSelectElement).value))"
      >
        <option v-for="n in props.pageSizeOptions ?? [20, 50]" :key="n" :value="n">{{ n }}</option>
      </select>
      条
    </label>
    <div class="ml-auto flex items-center gap-1">
      <button
        type="button"
        class="flex h-7 w-7 items-center justify-center rounded-md border disabled:cursor-not-allowed disabled:opacity-40"
        aria-label="上一页"
        :disabled="props.page <= 1"
        @click="emit('update:page', props.page - 1)"
      >
        <ChevronLeft class="h-4 w-4" />
      </button>
      <template v-for="(item, i) in pageItems" :key="i">
        <span v-if="item === '...'" class="px-1">…</span>
        <button
          v-else
          type="button"
          class="flex h-7 min-w-7 items-center justify-center rounded-md px-1 tabular-nums"
          :class="item === props.page
            ? 'bg-primary font-semibold text-primary-foreground'
            : 'border hover:bg-accent'"
          :aria-current="item === props.page ? 'page' : undefined"
          @click="emit('update:page', item)"
        >{{ item }}</button>
      </template>
      <button
        type="button"
        class="flex h-7 w-7 items-center justify-center rounded-md border disabled:cursor-not-allowed disabled:opacity-40"
        aria-label="下一页"
        :disabled="props.page >= props.pageCount"
        @click="emit('update:page', props.page + 1)"
      >
        <ChevronRight class="h-4 w-4" />
      </button>
    </div>
  </div>
</template>
