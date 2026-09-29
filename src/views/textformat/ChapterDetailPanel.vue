<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import Alert from '@/components/ui/Alert.vue'
import Badge from '@/components/ui/Badge.vue'
import Button from '@/components/ui/Button.vue'
import { AlertTriangle, Check, Loader2, RotateCcw } from 'lucide-vue-next'
import { formatNumber } from '@/utils/format'
import { confidenceClass, confidenceLabel, matterBrief, padChapterNum, reasonLabel } from '@/utils/bookLabels'
import type { WorkbenchChapter, WorkbenchMatter } from '@/api/textFormat'
import type { PreviewState } from '@/composables/useTextFormatWorkbench'

const props = withDefaults(defineProps<{
  chapter: WorkbenchChapter | null
  matters: WorkbenchMatter[]
  fileName: string | null
  preview: PreviewState
  marked: boolean
  marksBusy: boolean
  canRead: boolean
  hasPrev: boolean
  hasNext: boolean
  /** 章节号补齐位数（= 最大章节号位数），与章节表一致；缺省按 3 位。 */
  numPad?: number
  /** 同原编号组的规模/位置（详情结论卡与「查看同号章节」用）；非重复场景为 null。 */
  dupInfo?: { count: number; index: number } | null
}>(), { numPad: 3, dupInfo: null })
const emit = defineEmits<{
  (e: 'prev'): void
  (e: 'next'): void
  (e: 'mark'): void
  (e: 'show-same-number', origNum: number): void
}>()

// 原因卡展开状态：切章时收起，正文区滚动位置同步归零（不复用上一章的滚动）。
const expanded = ref<string[]>([])
const detailScroll = ref<HTMLElement | null>(null)
watch(() => props.chapter?.key, () => {
  expanded.value = []
  void nextTick().then(() => {
    if (detailScroll.value) detailScroll.value.scrollTop = 0
  })
})
const toggleExpand = (id: string) => {
  expanded.value = expanded.value.includes(id)
    ? expanded.value.filter((x) => x !== id)
    : [...expanded.value, id]
}
const isExpanded = (id: string) => expanded.value.includes(id)
const showSameNumber = computed(() => !!props.dupInfo && props.chapter != null && props.chapter.orig_num != null)
const briefTitle = (m: WorkbenchMatter) => matterBrief(m.reason, props.dupInfo).title
const briefSub = (m: WorkbenchMatter) => matterBrief(m.reason, props.dupInfo).sub
</script>

<template>
  <div class="flex h-full min-h-0 flex-col">
    <!-- 面板头 -->
    <div class="shrink-0 border-b px-5 py-3">
      <span class="text-sm font-medium">章节详情</span>
    </div>

    <div v-if="!props.chapter" class="flex flex-1 items-center justify-center p-6 text-center text-sm text-muted-foreground">
      在左侧表格中选择一章查看
    </div>
    <template v-else>
      <!-- 标题区 -->
      <div class="shrink-0 px-5 pb-2.5 pt-4">
        <div class="flex flex-wrap items-center gap-2">
          <h3 class="text-lg font-semibold leading-tight">
            第{{ padChapterNum(props.chapter.numStr || props.chapter.seq, props.numPad) }}章
            {{ props.chapter.title || '（无标题）' }}
          </h3>
          <Badge v-if="props.chapter.pending && !props.marked" variant="warning">待核对</Badge>
          <Badge v-if="props.marked" variant="success">已核对</Badge>
          <Badge v-if="props.chapter.adjusted" variant="outline">已调整</Badge>
        </div>
        <p class="mt-1 text-xs text-muted-foreground">{{ formatNumber(props.chapter.chars) }} 字</p>
      </div>

      <div ref="detailScroll" class="min-h-0 flex-1 space-y-3 overflow-y-auto py-4 px-5">
        <!-- 核对原因卡：默认只展示简短结论，完整处置说明放入卡内展开区 -->
        <div
          v-for="matter in props.matters"
          :key="matter.id"
          class="rounded-md border p-3 text-xs"
          :class="{ 'border-l-2 border-l-amber-400': matter.advisory }"
        >
          <div class="text-sm font-medium">{{ briefTitle(matter) }}</div>
          <p v-if="briefSub(matter)" class="mt-0.5 text-muted-foreground">{{ briefSub(matter) }}</p>
          <div class="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1">
            <button
              v-if="showSameNumber"
              type="button"
              class="text-primary hover:underline"
              @click="emit('show-same-number', props.chapter!.orig_num!)"
            >查看同号章节</button>
            <span v-if="showSameNumber" class="text-muted-foreground">·</span>
            <button type="button" class="text-primary hover:underline" @click="toggleExpand(matter.id)">
              {{ isExpanded(matter.id) ? '收起处理说明' : '展开处理说明' }}
            </button>
          </div>
          <div v-if="isExpanded(matter.id)" class="mt-2 space-y-1 border-t pt-2">
            <p>{{ matter.text }}</p>
            <p v-if="matter.detail" class="text-muted-foreground">{{ matter.detail }}</p>
          </div>
        </div>

        <!-- 正文预览 -->
        <div v-if="!props.canRead" class="flex h-24 items-center justify-center text-xs text-muted-foreground">
          该版本正文不可用，请下载或重新处理后查看
        </div>
        <div v-else-if="props.preview.status === 'loading'" class="flex h-24 items-center justify-center gap-2 text-sm text-muted-foreground">
          <Loader2 class="h-4 w-4 animate-spin" />
          加载正文中…
        </div>
        <Alert v-else-if="props.preview.status === 'error'" variant="destructive" class="text-xs">
          <AlertTriangle class="h-4 w-4 shrink-0" />
          <p>{{ props.preview.error }}</p>
        </Alert>
        <div
          v-else-if="props.preview.status === 'ready'"
          class="max-w-[68ch] whitespace-pre-wrap text-sm leading-7"
        >{{ props.preview.text }}</div>
        <div v-else class="flex h-24 items-center justify-center text-sm text-muted-foreground">
          加载正文中…
        </div>

        <!-- 处理说明：被调整章节展示调整明细；未调整章节保留结论与文件名入口。 -->
        <div class="space-y-1.5 rounded-md border p-3 text-xs">
          <div class="font-medium">处理说明</div>
          <template v-if="props.chapter.adjusted">
            <div class="flex flex-wrap items-center gap-1.5">
              <span v-if="props.chapter.orig_numStr" class="text-muted-foreground">
                原第{{ padChapterNum(props.chapter.orig_numStr, props.numPad) }}章 → 第{{ padChapterNum(props.chapter.final_num, props.numPad) }}章
              </span>
              <span
                class="inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium"
                :class="confidenceClass(props.chapter.confidence)"
              >
                置信{{ confidenceLabel(props.chapter.confidence) }}
              </span>
            </div>
            <ul v-if="props.chapter.reasons.length" class="list-disc space-y-0.5 pl-4 text-muted-foreground">
              <li v-for="(reason, i) in props.chapter.reasons" :key="i">{{ reasonLabel(reason) }}</li>
            </ul>
          </template>
          <p v-else class="text-muted-foreground">该章节未触发调整，按原结构直接保留。</p>
          <p v-if="props.fileName" class="truncate text-muted-foreground" :title="props.fileName">
            输出文件：{{ props.fileName }}
          </p>
        </div>
      </div>

      <!-- 操作行：上一章 / 标记已核对 / 下一章 -->
      <div class="flex shrink-0 items-center gap-2 border-t px-5 py-3">
        <Button variant="outline" size="sm" class="shrink-0" :disabled="!props.hasPrev" @click="emit('prev')">
          上一章
        </Button>
        <Button
          variant="outline"
          size="sm"
          class="min-w-[96px] shrink-0"
          :disabled="props.marksBusy"
          @click="emit('mark')"
        >
          <Check v-if="props.marked" class="h-4 w-4" />
          <RotateCcw v-else class="h-4 w-4" />
          {{ props.marked ? '撤销核对' : '标记已核对' }}
        </Button>
        <Button size="sm" class="shrink-0" :disabled="!props.hasNext" @click="emit('next')">
          下一章
        </Button>
      </div>
    </template>
  </div>
</template>
