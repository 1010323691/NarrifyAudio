<script setup lang="ts">
import { computed } from 'vue'
import Progress from '@/components/ui/Progress.vue'
import Button from '@/components/ui/Button.vue'
import { RefreshCw, XCircle } from 'lucide-vue-next'
import { formatNumber } from '@/utils/format'
import { padChapterNum } from '@/utils/bookLabels'
import { TONE_TEXT, type ParseRow } from '@/composables/useScriptParseWorkbench'

const props = defineProps<{
  rows: ParseRow[]
  selected: Record<string, boolean>
  selectedName: string | null
  /** 章节号补齐位数（= 最大编号位数）；缺省按 3 位显示。 */
  numPad?: number
  pageSize?: number
  /** 提交/批量取消在途时禁用勾选（在跑批次中改动会触发 409）。 */
  selectDisabled?: boolean
  emptyMessage?: string
}>()
const emit = defineEmits<{
  (e: 'select', name: string): void
  (e: 'toggle', name: string): void
  (e: 'retry', row: ParseRow): void
  (e: 'cancel', row: ParseRow): void
}>()

const chapterColClass = computed(() => ['w-20', 'w-24', 'w-28', 'w-32'][Math.min((props.numPad ?? 3) - 1, 3)] ?? 'w-32')
function chapterLabel(row: ParseRow): string {
  return padChapterNum(row.chapter.numStr, props.numPad ?? 3)
}
/** 失败/超时行副行文案（行内截断 + title 全文）。 */
function failText(row: ParseRow): string {
  return row.error || (row.status === 'timeout' ? '任务超时' : '解析失败')
}
</script>

<template>
  <table class="wb-chapter-table workbench-table" :class="{ 'wb-chapter-table--empty': !rows.length }" aria-label="章节解析列表">
    <thead>
      <tr>
        <th class="w-10" aria-label="选择" />
        <th class="whitespace-nowrap" :class="chapterColClass">章节</th>
        <th>标题</th>
        <th class="w-16 text-right">字数</th>
        <th class="w-72 whitespace-nowrap">解析状态</th>
      </tr>
    </thead>
    <tbody>
      <tr
        v-for="row in rows"
        :key="row.chapter.name"
        class="cursor-pointer"
        :data-state="row.chapter.name === props.selectedName ? 'selected' : undefined"
        @click="emit('select', row.chapter.name)"
      >
        <td>
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
        </td>
        <td class="whitespace-nowrap font-medium tabular-nums"><button type="button" class="chapter-select" :aria-pressed="row.chapter.name === props.selectedName" @click.stop="emit('select', row.chapter.name)">第{{ chapterLabel(row) }}章</button></td>
        <td>
          <span class="block truncate" :title="row.chapter.title">{{ row.chapter.title || '—' }}</span>
        </td>
        <td class="text-right tabular-nums">
          {{ row.chapter.chars ? formatNumber(row.chapter.chars) : '—' }}
        </td>
        <td>
          <!-- 状态单元格单行布局：状态文字 + 进度 + 原因（截断）+ 操作按钮。
               提示文字与状态同行（truncate + title 全文），不占独立行高。 -->
          <div class="flex min-w-0 items-center gap-2">
            <span
              class="shrink-0 text-xs font-semibold leading-normal"
              :class="TONE_TEXT[row.tone]"
              :title="row.error || undefined"
            >
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
        </td>
      </tr>
      <tr v-if="!rows.length">
        <td :colspan="5" class="h-20 text-center text-sm text-muted-foreground">
          {{ emptyMessage ?? '没有符合条件的章节' }}
        </td>
      </tr>
    </tbody>
  </table>
</template>

<style scoped>
/* Match the voices list: native table, one outer scroller, no row component wrappers. */
.wb-chapter-table { width:100%; table-layout:fixed; border-collapse:collapse; font-size:12px; line-height:18px; }
.wb-chapter-table th.text-right { text-align:right; }
.wb-chapter-table tbody tr:hover { background:hsl(var(--muted) / .5); }
.wb-chapter-table tr[data-state='selected'] { background:hsl(var(--primary) / .08); box-shadow:inset 3px 0 hsl(var(--primary)); }
.wb-chapter-table td[colspan] { height:80px; }
.wb-chapter-table--empty { height:100%; }
.wb-chapter-table--empty td[colspan] { height:auto; border-bottom:0; }
.chapter-select { text-align:left; }
.chapter-select:focus-visible { outline:2px solid hsl(var(--ring)); outline-offset:2px; border-radius:3px; }
@media(pointer:coarse) { .chapter-select { min-height:44px; } }
@media(prefers-reduced-motion:reduce) { .wb-chapter-table :deep(*) { transition:none; } }
</style>
