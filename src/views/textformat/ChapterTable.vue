<script setup lang="ts">
import Table from '@/components/ui/Table.vue'
import TableBody from '@/components/ui/TableBody.vue'
import TableCell from '@/components/ui/TableCell.vue'
import TableHead from '@/components/ui/TableHead.vue'
import TableHeader from '@/components/ui/TableHeader.vue'
import TableRow from '@/components/ui/TableRow.vue'
import Badge from '@/components/ui/Badge.vue'
import { formatNumber } from '@/utils/format'
import type { WorkbenchChapter } from '@/api/textFormat'

const props = withDefaults(defineProps<{
  chapters: WorkbenchChapter[]
  selectedKey: string | null
  /** 缺省（如 HMR 过渡期父组件未升级）时降级为 —，避免渲染崩溃。 */
  reasonBriefs?: Record<string, string>
}>(), { reasonBriefs: () => ({}) })
const emit = defineEmits<{
  (e: 'select', key: string): void
}>()

const reasonBrief = (chapter: WorkbenchChapter) => props.reasonBriefs[chapter.key ?? ''] ?? '—'
</script>

<template>
  <Table class="wb-chapter-table">
    <TableHeader>
      <TableRow>
        <TableHead class="w-20">章节</TableHead>
        <TableHead>标题</TableHead>
        <TableHead class="w-20 text-right">字数</TableHead>
        <TableHead class="w-24">处理结果</TableHead>
        <TableHead class="w-28">核对原因</TableHead>
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
        <TableCell class="font-medium tabular-nums">第{{ chapter.numStr || String(chapter.seq).padStart(3, '0') }}章</TableCell>
        <TableCell>
          <span class="block truncate" :title="chapter.title">{{ chapter.title || '—' }}</span>
        </TableCell>
        <TableCell class="text-right tabular-nums">{{ formatNumber(chapter.chars) }}</TableCell>
        <TableCell>
          <Badge v-if="chapter.pending" variant="warning">待核对</Badge>
          <Badge v-else-if="chapter.adjusted" variant="success">已调整</Badge>
          <Badge v-else variant="outline">正常</Badge>
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
/* 模块内边距：表头/单元格左右 16px，与筛选行、分页器对齐。 */
.wb-chapter-table :deep(th) {
  padding-left: 1rem;
  padding-right: 1rem;
}
.wb-chapter-table :deep(td) {
  padding-left: 1rem;
  padding-right: 1rem;
}
/* 固定布局：章节/字数/处理结果列宽写死，标题列吃掉全部剩余宽度，不留右侧空白。 */
.wb-chapter-table :deep(table) {
  table-layout: fixed;
}
</style>
