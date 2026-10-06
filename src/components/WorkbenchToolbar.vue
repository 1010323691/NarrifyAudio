<script setup lang="ts">
import Input from '@/components/ui/Input.vue'
import Button from '@/components/ui/Button.vue'
import { Search, RefreshCw } from 'lucide-vue-next'
defineProps<{
  query: string
  filter: string
  filters: { key: string; label: string; count: number }[]
  placeholder?: string
  loading?: boolean
  refreshDisabled?: boolean
}>()
const emit = defineEmits<{
  'update:query': [value: string]
  'update:filter': [value: string]
  refresh: []
}>()
</script>

<template>
  <div class="workbench-toolbar">
    <div class="workbench-search-row">
      <label class="relative min-w-0 flex-1">
        <span class="sr-only">{{ placeholder || '搜索文件或章节' }}</span>
        <Search class="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
        <Input :model-value="query" class="h-8 pl-8 text-xs" :placeholder="placeholder || '搜索文件或章节…'" @update:model-value="emit('update:query', String($event))" />
      </label>
      <Button variant="ghost" class="h-8 w-8 shrink-0 p-0" aria-label="刷新列表" title="刷新列表" :disabled="loading || refreshDisabled" @click="emit('refresh')">
        <RefreshCw class="h-3.5 w-3.5" :class="{ 'animate-spin': loading }" />
      </Button>
    </div>
    <div class="workbench-filter-row" role="group" aria-label="状态筛选">
      <button v-for="item in filters" :key="item.key" type="button" class="workbench-filter" :aria-pressed="filter === item.key" @click="emit('update:filter', item.key)">
        {{ item.label }} <span class="tabular-nums">{{ item.count }}</span>
      </button>
      <slot name="filters" />
    </div>
    <div v-if="$slots.selection" class="workbench-selection-row" role="group" aria-label="批量选择"><slot name="selection" /></div>
    <p class="mobile-table-hint">左右滑动表格查看完整信息，点击名称查看详情</p>
  </div>
</template>

<style scoped>
.workbench-toolbar { flex-shrink:0; border-bottom:1px solid hsl(var(--border)); }
.workbench-search-row, .workbench-filter-row, .workbench-selection-row { display:flex; align-items:center; gap:8px; padding:8px 12px; }
.workbench-filter-row, .workbench-selection-row { flex-wrap:wrap; }
.workbench-filter-row { padding-top:0; }
.workbench-selection-row { border-top:1px solid hsl(var(--border)); padding:4px 12px; }
.workbench-filter { display:inline-flex; align-items:center; gap:4px; min-height:32px; padding:4px 10px; border-radius:999px; font-size:12px; color:hsl(var(--muted-foreground)); }
.workbench-filter:hover { background:hsl(var(--muted)); }
.workbench-filter[aria-pressed=true] { color:hsl(var(--primary)); background:hsl(var(--primary) / .1); font-weight:600; }
.workbench-toolbar :is(button,input,select):focus-visible { outline:2px solid hsl(var(--ring)); outline-offset:2px; }
</style>
