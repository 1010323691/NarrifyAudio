<script setup lang="ts">
import Table from '@/components/ui/Table.vue'
import TableBody from '@/components/ui/TableBody.vue'
import TableCell from '@/components/ui/TableCell.vue'
import TableHead from '@/components/ui/TableHead.vue'
import TableHeader from '@/components/ui/TableHeader.vue'
import TableRow from '@/components/ui/TableRow.vue'
import Progress from '@/components/ui/Progress.vue'
import Button from '@/components/ui/Button.vue'
import { RefreshCw, XCircle } from 'lucide-vue-next'
import { formatNumber } from '@/utils/format'
import { padChapterNum } from '@/utils/bookLabels'
import { TONE_CHIP, type ParseRow } from '@/composables/useScriptParseWorkbench'

const props = defineProps<{
  rows: ParseRow[]
  selected: Record<string, boolean>
  selectedName: string | null
  /** 章节号补齐位数（= 最大编号位数）；缺省按 3 位显示。 */
  numPad?: number
  /** 提交/批量取消在途时禁用勾选（在跑批次中改动会触发 409）。 */
  selectDisabled?: boolean
}>()
const emit = defineEmits<{
  (e: 'select', name: string): void
  (e: 'toggle', name: string): void
  (e: 'retry', row: ParseRow): void
  (e: 'cancel', row: ParseRow): void
}>()

const chapterColClass = ['w-20', 'w-24', 'w-28', 'w-32'][Math.min((props.numPad ?? 3) - 1, 3)] ?? 'w-32'
function chapterLabel(row: ParseRow): string {
  return padChapterNum(row.chapter.numStr, props.numPad ?? 3)
}
/** 失败/超时行副行文案（行内截断 + title 全文）。 */
function failText(row: ParseRow): string {
  return row.error || (row.status === 'timeout' ? '任务超时' : '解析失败')
}
</script>

<template>
  <Table class="wb-chapter-table">
    <TableHeader>
      <TableRow>
        <TableHead class="w-10" aria-label="选择" />
        <TableHead class="whitespace-nowrap" :class="chapterColClass">章节</TableHead>
        <TableHead>标题</TableHead>
        <TableHead class="w-16 text-right">字数</TableHead>
        <TableHead class="w-72 whitespace-nowrap">解析状态</TableHead>
      </TableRow>
    </TableHeader>
    <TableBody>
      <TableRow
        v-for="row in rows"
        :key="row.chapter.name"
        class="cursor-pointer"
        :data-state="row.chapter.name === props.selectedName ? 'selected' : undefined"
        @click="emit('select', row.chapter.name)"
      >
        <TableCell>
          <!-- 复选框 @click.stop：点勾选不连带触发行选中。 -->
          <input
            type="checkbox"
            class="h-4 w-4 cursor-pointer accent-primary"
            :aria-label="`选择第${row.chapter.numStr}章`"
            :checked="!!props.selected[row.chapter.name]"
            :disabled="props.selectDisabled"
            @click.stop
            @change="emit('toggle', row.chapter.name)"
          />
        </TableCell>
        <TableCell class="whitespace-nowrap font-medium tabular-nums">第{{ chapterLabel(row) }}章</TableCell>
        <TableCell>
          <span class="block truncate" :title="row.chapter.title">{{ row.chapter.title || '—' }}</span>
        </TableCell>
        <TableCell class="text-right tabular-nums">
          {{ row.chapter.chars ? formatNumber(row.chapter.chars) : '—' }}
        </TableCell>
        <TableCell>
          <!-- 状态单元格单行布局：chip + 进度 + 原因（截断）+ 操作按钮。
               提示文字与 chip 同行（truncate + title 全文），不占独立行高。 -->
          <div class="flex min-w-0 items-center gap-2">
            <span
              class="inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold leading-none tracking-wide transition-colors"
              :class="TONE_CHIP[row.tone].chip"
              :title="row.error || undefined"
            >
              <span class="h-1.5 w-1.5 shrink-0 rounded-full" :class="TONE_CHIP[row.tone].dot" aria-hidden="true" />
              {{ row.label }}
            </span>
            <template v-if="row.status === 'active'">
              <!-- 暂停（等待 LLM 恢复）时条/百分比不渲染：冻结值无信息量，且长 label 下整行放不下。 -->
              <template v-if="row.label !== '等待 LLM 恢复'">
                <Progress :value="row.progress" class="h-1.5 w-12 shrink-0" />
                <span class="w-8 shrink-0 text-right text-xs tabular-nums text-muted-foreground">
                  {{ Math.round(row.progress * 100) }}%
                </span>
              </template>
            </template>
            <span
              v-if="row.status === 'failed' || row.status === 'timeout'"
              class="min-w-0 flex-1 truncate text-xs text-destructive/80"
              :title="failText(row)"
            >
              {{ failText(row) }}
            </span>
            <Button
              v-if="row.status === 'active'"
              variant="outline"
              size="sm"
              class="shrink-0 h-[21px] gap-1 rounded-md px-1.5 text-[11px]"
              @click.stop="emit('cancel', row)"
            >
              <XCircle class="h-3 w-3" />取消
            </Button>
            <Button
              v-else-if="row.retryable"
              variant="outline"
              size="sm"
              class="shrink-0 h-[21px] gap-1 rounded-md px-1.5 text-[11px]"
              @click.stop="emit('retry', row)"
            >
              <RefreshCw class="h-3 w-3" />重试
            </Button>
          </div>
        </TableCell>
      </TableRow>
      <TableRow v-if="!rows.length">
        <TableCell :colspan="5" class="h-20 text-center text-sm text-muted-foreground">
          没有符合条件的章节
        </TableCell>
      </TableRow>
    </TableBody>
  </Table>
</template>

<style scoped>
/* 模块内边距：与排版工作台章节表同款（对齐筛选行/分页器，行高收窄多留行数）。 */
.wb-chapter-table :deep(th) {
  padding-left: 1rem;
  padding-right: 1rem;
  padding-top: 0.375rem;
  padding-bottom: 0.375rem;
  height: auto;
}
/* td 上下 12.575px（子像素，勿取整）：选择行下沉底栏后滚动区 504px − 表头 32.5px = 471.5px，
   10 行对齐整页 ⇒ 行高必须 = 471.5 ÷ 10 = 47.15px = 内容 22px + 2×12.575px。
   带按钮行的取消/重试按钮压到 21px（h-[21px] 覆盖 sm 的 h-9，与状态 chip 21px 齐平），
   解析中/失败行与常态行同为 47.14px，不再比常态行高出 14px。窗口高度变化后对齐会漂移，属固有约束。 */
.wb-chapter-table :deep(td) {
  padding-left: 1rem;
  padding-right: 1rem;
  padding-top: 12.575px;
  padding-bottom: 12.575px;
}
/* 固定布局：章节/字数/状态列宽写死，标题列吃掉剩余宽度。 */
.wb-chapter-table :deep(table) {
  table-layout: fixed;
}
/* 表头吸顶：Table 组件根 div 的 overflow-auto 会让 thead 相对它（不滚）定位而失效，
   改 visible 后 sticky 相对外层滚动容器（wb-main 的 overflow-y-auto）生效，
   表头停在筛选/选择行正下方；底色用工作区同款近不透明 card 填充防透行。 */
.wb-chapter-table {
  overflow: visible;
}
.wb-chapter-table :deep(thead th) {
  position: sticky;
  top: 0;
  z-index: 1;
  background-color: hsl(var(--card) / 0.95);
}
.wb-chapter-table :deep(tr[data-state='selected']) {
  background-color: hsl(var(--primary) / 0.08);
}
</style>
