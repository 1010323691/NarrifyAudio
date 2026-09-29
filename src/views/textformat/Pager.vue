<script setup lang="ts">
import { computed } from 'vue'
import { cn } from '@/lib/utils'

const props = defineProps<{
  page: number
  pageCount: number
  total: number
  pageSize: number
  pageSizeOptions?: number[]
  class?: string
}>()
const emit = defineEmits<{
  (e: 'update:page', page: number): void
  (e: 'update:pageSize', size: number): void
}>()

const rangeText = computed(() => {
  if (!props.total) return '共 0 项'
  const start = (props.page - 1) * props.pageSize + 1
  const end = Math.min(props.total, props.page * props.pageSize)
  return `${start}–${end} / 共 ${props.total} 项`
})
</script>

<template>
  <div :class="cn('flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-muted-foreground', props.class)">
    <span>{{ rangeText }}</span>
    <label class="flex items-center gap-1.5">
      每页
      <select
        class="h-7 rounded-md border bg-background px-1.5 text-xs"
        :value="props.pageSize"
        @change="emit('update:pageSize', Number(($event.target as HTMLSelectElement).value))"
      >
        <option v-for="n in props.pageSizeOptions ?? [20, 50]" :key="n" :value="n">{{ n }}</option>
      </select>
      项
    </label>
    <div class="ml-auto flex items-center gap-1">
      <button
        type="button"
        class="h-7 rounded-md border px-2 disabled:cursor-not-allowed disabled:opacity-40"
        :disabled="props.page <= 1"
        @click="emit('update:page', props.page - 1)"
      >上一页</button>
      <span class="px-1">{{ Math.min(props.page, props.pageCount) }} / {{ props.pageCount }}</span>
      <button
        type="button"
        class="h-7 rounded-md border px-2 disabled:cursor-not-allowed disabled:opacity-40"
        :disabled="props.page >= props.pageCount"
        @click="emit('update:page', props.page + 1)"
      >下一页</button>
    </div>
  </div>
</template>
