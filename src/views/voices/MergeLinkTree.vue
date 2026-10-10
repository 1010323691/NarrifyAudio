<script setup lang="ts">
// 指向树：缩进列表，无复选框——点角色名区域切换选中，独立的箭头按钮负责展开/收起。
import { Check, ChevronRight } from 'lucide-vue-next'
import Badge from '@/components/ui/Badge.vue'
import Button from '@/components/ui/Button.vue'
import MiniAudioPlayer from '@/components/ui/MiniAudioPlayer.vue'
import type { TreeRow } from '@/composables/batchMergeGraph'

defineProps<{
  rows: TreeRow[]
  selected: Set<string>
  busy: boolean
  previewUrl: (relative: string) => string
}>()
const emit = defineEmits<{
  toggle: [name: string]
  expand: [name: string]
  veto: [name: string, target: string]
}>()

/** → opens, ← closes; the key is only consumed when it actually toggles the branch. */
function arrow(event: KeyboardEvent, row: TreeRow, open: boolean) {
  if (!row.hasChildren || row.expanded === open) return
  event.preventDefault()
  emit('expand', row.name)
}
const indent = (row: TreeRow) => `${(Math.min(row.depth + 1, 5) - 1) * 16}px`
const genderLabel = (g: string) => (g === 'male' ? '男' : g === 'female' ? '女' : '未知')
</script>

<template>
  <ul class="merge-tree" aria-label="待合并角色">
    <li v-for="row in rows" :key="row.name" class="merge-row" :class="{ 'is-selected': selected.has(row.name) }" :style="{ paddingLeft: indent(row) }">
      <button
        v-if="row.hasChildren" type="button" class="merge-expander" :aria-expanded="row.expanded"
        :aria-label="`${row.expanded ? '收起' : '展开'} ${row.name} 的上游角色`" @click="emit('expand', row.name)"
      >
        <ChevronRight class="h-4 w-4 transition-transform" :class="{ 'rotate-90': row.expanded }" />
      </button>
      <span v-else class="merge-expander" aria-hidden="true" />
      <button
        type="button" class="merge-main" :aria-pressed="selected.has(row.name)" :disabled="busy"
        @click="emit('toggle', row.name)"
        @keydown.right="arrow($event, row, true)"
        @keydown.left="arrow($event, row, false)"
      >
        <span class="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span v-if="row.depth >= 5" class="merge-level">L{{ row.depth + 1 }}</span>
          <span class="merge-name">{{ row.name }}</span>
          <span class="text-[11px] text-muted-foreground">{{ genderLabel(row.role.gender) }} · {{ row.role.line_count }} 句</span>
          <Badge v-if="row.isNew">新增</Badge>
          <Badge v-if="row.upstreamCount" variant="secondary">上游 {{ row.upstreamCount }}</Badge>
          <Badge v-if="row.selectedBelow" variant="outline">含已选 {{ row.selectedBelow }}</Badge>
          <Badge v-if="row.containsNew" variant="warning">含新增</Badge>
        </span>
        <span v-if="row.linkTarget" class="merge-basis">
          {{ row.basis || '疑似同一角色' }} · 指向 {{ row.linkTarget }}<template v-if="row.targetSample">：「{{ row.targetSample }}」</template>
        </span>
      </button>
      <Check v-if="selected.has(row.name)" class="h-4 w-4 shrink-0 text-primary" aria-hidden="true" />
      <MiniAudioPlayer v-if="row.role.preview" :src="previewUrl(row.role.preview)" class="shrink-0" />
      <Button variant="ghost" size="sm" class="shrink-0 text-xs" :disabled="busy || !row.linkTarget" :aria-label="`忽略：${row.name} 不是 ${row.linkTarget}`" @click="emit('veto', row.name, row.linkTarget)">忽略</Button>
    </li>
  </ul>
</template>

<style scoped>
.merge-tree { display:flex; flex-direction:column; gap:4px; margin:0; padding:0; list-style:none; }
.merge-row { display:flex; align-items:center; gap:6px; padding-top:4px; padding-bottom:4px; padding-right:6px; border:1px solid transparent; border-radius:8px; }
.merge-row:hover { background:hsl(var(--accent) / .5); }
.merge-row.is-selected { border-color:hsl(var(--primary)); background:hsl(var(--primary) / .08); }
.merge-expander { display:inline-flex; align-items:center; justify-content:center; flex:0 0 28px; height:28px; border-radius:6px; }
button.merge-expander:hover { background:hsl(var(--accent)); }
.merge-main { display:flex; flex-direction:column; align-items:flex-start; gap:2px; flex:1; min-width:0; padding:2px 4px; text-align:left; border-radius:6px; }
.merge-name { font-size:13px; font-weight:500; overflow-wrap:anywhere; }
.is-selected .merge-name { font-weight:700; color:hsl(var(--primary)); }
.merge-level { padding:0 4px; border-radius:4px; background:hsl(var(--muted)); font-size:10px; }
.merge-basis { font-size:11px; color:hsl(var(--muted-foreground)); overflow-wrap:anywhere; }
button:focus-visible { outline:2px solid hsl(var(--ring)); outline-offset:2px; }
</style>
