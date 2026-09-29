<script setup lang="ts">
import Table from '@/components/ui/Table.vue'
import TableBody from '@/components/ui/TableBody.vue'
import TableCell from '@/components/ui/TableCell.vue'
import TableHead from '@/components/ui/TableHead.vue'
import TableHeader from '@/components/ui/TableHeader.vue'
import TableRow from '@/components/ui/TableRow.vue'
import Button from '@/components/ui/Button.vue'
import Badge from '@/components/ui/Badge.vue'
import { Download, Eye } from 'lucide-vue-next'
import { formatNumber } from '@/utils/format'
import { confidenceLabel } from '@/utils/bookLabels'
import type { WorkbenchChapter } from '@/api/textFormat'

const props = defineProps<{
  chapters: WorkbenchChapter[]
  selectedKey: string | null
  marks: string[]
  canRead: boolean
  marksBusyKey: string | null
}>()
const emit = defineEmits<{
  (e: 'select', key: string): void
  (e: 'download', chapter: WorkbenchChapter): void
}>()
</script>

<template>
  <Table>
    <TableHeader>
      <TableRow>
        <TableHead class="w-12">序号</TableHead>
        <TableHead>标题</TableHead>
        <TableHead class="w-20 text-right">字数</TableHead>
        <TableHead class="w-36">状态</TableHead>
        <TableHead class="w-24 text-right">操作</TableHead>
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
        <TableCell class="text-muted-foreground tabular-nums">{{ chapter.seq }}</TableCell>
        <TableCell class="max-w-[320px]">
          <span class="block truncate" :title="chapter.title">{{ chapter.title || '—' }}</span>
        </TableCell>
        <TableCell class="text-right tabular-nums">{{ formatNumber(chapter.chars) }}</TableCell>
        <TableCell>
          <div class="flex flex-wrap items-center gap-1">
            <Badge v-if="chapter.pending" variant="warning">待核对</Badge>
            <Badge v-else-if="chapter.adjusted" variant="outline">已调整</Badge>
            <Badge v-else variant="success">正常</Badge>
            <Badge v-if="props.marks.includes(chapter.key ?? '')" variant="secondary">已核对</Badge>
            <span
              v-if="chapter.confidence && chapter.confidence !== 'high' && chapter.reasons.some((r) => r !== 'kept')"
              class="text-[11px] text-muted-foreground"
            >
              置信{{ confidenceLabel(chapter.confidence) }}
            </span>
          </div>
        </TableCell>
        <TableCell class="text-right">
          <div class="flex items-center justify-end gap-1" @click.stop>
            <Button
              variant="ghost"
              size="sm"
              class="h-7 px-2"
              :disabled="!props.canRead"
              :title="props.canRead ? '查看正文' : '该版本正文不可用'"
              @click="chapter.key && emit('select', chapter.key)"
            >
              <Eye class="h-3.5 w-3.5" />
            </Button>
            <Button
              variant="ghost"
              size="sm"
              class="h-7 px-2"
              :disabled="!props.canRead || props.marksBusyKey === chapter.key"
              :title="props.canRead ? '下载本章' : '该版本正文不可用'"
              @click="emit('download', chapter)"
            >
              <Download class="h-3.5 w-3.5" />
            </Button>
          </div>
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
