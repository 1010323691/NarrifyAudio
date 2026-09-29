<script setup lang="ts">
import Table from '@/components/ui/Table.vue'
import TableBody from '@/components/ui/TableBody.vue'
import TableCell from '@/components/ui/TableCell.vue'
import TableHead from '@/components/ui/TableHead.vue'
import TableHeader from '@/components/ui/TableHeader.vue'
import TableRow from '@/components/ui/TableRow.vue'
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
  /** 已核对章节 key：核对状态徽标随标记实时同步（pending 是后端静态字段）。 */
  markedKeys?: Set<string>
}>(), { reasonBriefs: () => ({}) , numPad: 3, markedKeys: () => new Set<string>() })
const emit = defineEmits<{
  (e: 'select', key: string): void
}>()

const reasonBrief = (chapter: WorkbenchChapter) => props.reasonBriefs[chapter.key ?? ''] ?? '—'
const chapterLabel = (chapter: WorkbenchChapter) =>
  padChapterNum(chapter.numStr || chapter.seq, props.numPad)
/** 章节列宽随编号位数增宽（第001章 比 第1章 更宽），避免定宽不够换行。 */
const chapterColClass = ['w-20', 'w-24', 'w-28', 'w-32'][Math.min(props.numPad - 1, 3)] ?? 'w-32'
</script>

<template>
  <Table class="wb-chapter-table">
    <TableHeader>
      <TableRow>
        <TableHead class="whitespace-nowrap" :class="chapterColClass">章节</TableHead>
        <TableHead>标题</TableHead>
        <TableHead class="w-16 text-right">字数</TableHead>
        <TableHead class="wb-col-status w-28 whitespace-nowrap">核对状态</TableHead>
        <TableHead class="w-48 whitespace-nowrap">核对原因</TableHead>
      </TableRow>
    </TableHeader>
    <TableBody>
      <TableRow
        v-for="chapter in chapters"
        :key="chapter.key ?? chapter.seq"
        class="cursor-pointer"
        :data-state="chapter.key === props.selectedKey ? 'selected' : undefined"
        @click="chapter.key && emit('select', chapter.key)"
      >
        <TableCell class="whitespace-nowrap font-medium tabular-nums">第{{ chapterLabel(chapter) }}章</TableCell>
        <TableCell>
          <span class="block truncate" :title="chapter.title">{{ chapter.title || '—' }}</span>
        </TableCell>
        <TableCell class="text-right tabular-nums">{{ formatNumber(chapter.chars) }}</TableCell>
        <TableCell class="wb-col-status">
          <Badge v-if="chapter.key && props.markedKeys.has(chapter.key)" variant="success">已核对</Badge>
          <Badge v-else-if="chapter.pending" variant="warning">待核对</Badge>
          <Badge v-else-if="chapter.adjusted" variant="success">已调整</Badge>
          <!-- 正常章节用与已核对相同的绿色徽标，整列状态一眼可辨。 -->
          <Badge v-else variant="success">正常</Badge>
        </TableCell>
        <TableCell class="text-muted-foreground">
          <span class="block truncate" :title="reasonBrief(chapter)">{{ reasonBrief(chapter) }}</span>
        </TableCell>
      </TableRow>
      <TableRow v-if="!chapters.length">
        <TableCell :colspan="5" class="h-20 text-center text-sm text-muted-foreground">
          没有符合条件的章节
        </TableCell>
      </TableRow>
    </TableBody>
  </Table>
</template>

<style scoped>
/* 模块内边距：表头/单元格左右 16px，与筛选行、分页器对齐；上下收窄让一行更矮，
   默认视口下可不滚动显示更多行。 */
.wb-chapter-table :deep(th) {
  padding-left: 1rem;
  padding-right: 1rem;
  padding-top: 0.375rem;
  padding-bottom: 0.375rem;
  height: auto;
}
/* td 上下 12.325px（子像素，勿取整）：与文本解析页同骨架，滚动区 504px − 表头 32.5px
   = 471.5px，10 行对齐整页 ⇒ 行高 = 471.5 ÷ 10 = 47.15px = 内容 22.5px + 2×12.325px。
   窗口高度变化后对齐会漂移，属固有约束。 */
.wb-chapter-table :deep(td) {
  padding-left: 1rem;
  padding-right: 1rem;
  padding-top: 12.325px;
  padding-bottom: 12.325px;
}
/* 核对状态列内容整体右移一点点，拉开与字数列的间距（20px > 基线 16px）。 */
.wb-chapter-table :deep(th.wb-col-status),
.wb-chapter-table :deep(td.wb-col-status) {
  padding-left: 1.25rem;
}
/* 固定布局：章节/字数/处理结果/核对原因列宽写死，标题列吃掉剩余宽度——
   右侧列组贴住表格右缘，不留大片右侧空白。 */
.wb-chapter-table :deep(table) {
  table-layout: fixed;
}
/* 选中行高亮：列表与详情同步定位后，选中行要一眼可辨。 */
.wb-chapter-table :deep(tr[data-state='selected']) {
  background-color: hsl(var(--primary) / 0.08);
}
</style>
