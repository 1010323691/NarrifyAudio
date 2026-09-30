<script setup lang="ts">
import { computed, ref } from 'vue'
import { useTenRowHeight } from '@/composables/useTenRowHeight'
import Badge from '@/components/ui/Badge.vue'
import { formatNumber } from '@/utils/format'
import { padChapterNum } from '@/utils/bookLabels'
import type { WorkbenchChapter } from '@/api/textFormat'

const props = withDefaults(defineProps<{
  chapters: WorkbenchChapter[]
  selectedKey: string | null
  /** 缺省（如 HMR 过渡期父组件未升级）时降级为 —，避免渲染崩溃。 */
  reasonBriefs?: Record<string, string>
  /** 章节号补齐位数（= 最大章节号位数）；缺省按 3 位显示。 */
  numPad?: number
  pageSize?: number
  /** 已核对章节 key：核对状态徽标随标记实时同步（pending 是后端静态字段）。 */
  markedKeys?: Set<string>
}>(), { reasonBriefs: () => ({}) , numPad: 3, pageSize: 10, markedKeys: () => new Set<string>() })
const table = ref<HTMLTableElement | null>(null)
const rowHeight = useTenRowHeight(table, () => props.pageSize)
const emit = defineEmits<{
  (e: 'select', key: string): void
}>()

const reasonBrief = (chapter: WorkbenchChapter) => props.reasonBriefs[chapter.key ?? ''] ?? '—'
const chapterLabel = (chapter: WorkbenchChapter) =>
  padChapterNum(chapter.numStr || chapter.seq, props.numPad)
/** 章节列宽随编号位数增宽（第001章 比 第1章 更宽），避免定宽不够换行。 */
const chapterColClass = computed(() => ['w-20', 'w-24', 'w-28', 'w-32'][Math.min(props.numPad - 1, 3)] ?? 'w-32')
</script>

<template>
  <table ref="table" class="wb-chapter-table" :style="{ '--chapter-row-height': `${rowHeight}px` }" aria-label="章节核对列表">
    <thead>
      <tr>
        <th class="whitespace-nowrap" :class="chapterColClass">章节</th>
        <th>标题</th>
        <th class="w-16 text-right">字数</th>
        <th class="wb-col-status w-28 whitespace-nowrap">核对状态</th>
        <th class="w-48 whitespace-nowrap">核对原因</th>
      </tr>
    </thead>
    <tbody>
      <tr
        v-for="chapter in chapters"
        :key="chapter.key ?? chapter.seq"
        class="cursor-pointer"
        :data-state="chapter.key === props.selectedKey ? 'selected' : undefined"
        @click="chapter.key && emit('select', chapter.key)"
      >
        <td class="whitespace-nowrap font-medium tabular-nums"><button type="button" class="chapter-select" :disabled="!chapter.key" :aria-pressed="chapter.key === props.selectedKey" @click.stop="chapter.key && emit('select', chapter.key)">第{{ chapterLabel(chapter) }}章</button></td>
        <td>
          <span class="block truncate" :title="chapter.title">{{ chapter.title || '—' }}</span>
        </td>
        <td class="text-right tabular-nums">{{ formatNumber(chapter.chars) }}</td>
        <td class="wb-col-status">
          <Badge v-if="chapter.key && props.markedKeys.has(chapter.key)" variant="success">已核对</Badge>
          <Badge v-else-if="chapter.pending" variant="warning">待核对</Badge>
          <Badge v-else-if="chapter.adjusted" variant="success">已调整</Badge>
          <!-- 正常章节用与已核对相同的绿色徽标，整列状态一眼可辨。 -->
          <Badge v-else variant="success">正常</Badge>
        </td>
        <td class="text-muted-foreground">
          <span class="block truncate" :title="reasonBrief(chapter)">{{ reasonBrief(chapter) }}</span>
        </td>
      </tr>
      <tr v-if="!chapters.length">
        <td :colspan="5" class="h-20 text-center text-sm text-muted-foreground">
          没有符合条件的章节
        </td>
      </tr>
    </tbody>
  </table>
</template>

<style scoped>
/* Match the voices list: native table, one outer scroller, no row component wrappers. */
.wb-chapter-table { width:100%; table-layout:fixed; border-collapse:collapse; font-size:12px; line-height:18px; }
.wb-chapter-table th { position:sticky; top:0; z-index:1; height:28px; padding:5px 12px; text-align:left; font-size:11px; line-height:18px; font-weight:500; color:hsl(var(--muted-foreground)); background:hsl(var(--card)); }
.wb-chapter-table td { height:var(--chapter-row-height, 32px); padding:3px 12px; vertical-align:middle; border-bottom:1px solid hsl(var(--border) / .6); }
.wb-chapter-table th.text-right { text-align:right; }
.wb-chapter-table tbody tr:hover { background:hsl(var(--muted) / .5); }
.wb-chapter-table tr[data-state='selected'] { background:hsl(var(--primary) / .08); box-shadow:inset 3px 0 hsl(var(--primary)); }
.wb-chapter-table td[colspan] { height:80px; }
.chapter-select { text-align:left; }
.chapter-select:focus-visible { outline:2px solid hsl(var(--ring)); outline-offset:2px; border-radius:3px; }
@media(pointer:coarse) { .chapter-select { min-height:44px; } }
@media(prefers-reduced-motion:reduce) { .wb-chapter-table :deep(*) { transition:none; } }
</style>
