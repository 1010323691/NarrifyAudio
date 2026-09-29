<script setup lang="ts">
import { ref } from 'vue'
import Alert from '@/components/ui/Alert.vue'
import Badge from '@/components/ui/Badge.vue'
import Button from '@/components/ui/Button.vue'
import { AlertTriangle, Check, Download, Info, Loader2, RotateCcw } from 'lucide-vue-next'
import { formatNumber } from '@/utils/format'
import { confidenceClass, confidenceLabel } from '@/utils/bookLabels'
import type { WorkbenchChapter, WorkbenchMatter } from '@/api/textFormat'
import type { PreviewState } from '@/composables/useTextFormatWorkbench'

const props = defineProps<{
  chapter: WorkbenchChapter | null
  matters: WorkbenchMatter[]
  fileName: string | null
  preview: PreviewState
  marked: boolean
  marksBusy: boolean
  canRead: boolean
  hasPrev: boolean
  hasNext: boolean
}>()
const emit = defineEmits<{
  (e: 'prev'): void
  (e: 'next'): void
  (e: 'mark'): void
  (e: 'download-all'): void
}>()

// 参考布局：事项卡常驻，正文 / 处理说明在面板内以标签切换。
const panelTab = ref<'body' | 'notes'>('body')
</script>

<template>
  <div class="flex h-full min-h-0 flex-col">
    <!-- 面板头：章节详情 + 下载全部 -->
    <div class="flex shrink-0 items-center justify-between gap-2 border-b px-4 py-2.5">
      <span class="text-sm font-medium">章节详情</span>
      <Button
        variant="outline"
        size="sm"
        class="h-7 gap-1.5 px-2 text-xs"
        :disabled="!props.canRead"
        :title="props.canRead ? '下载全部章节（完整 ZIP）' : '该版本正文不可用'"
        @click="emit('download-all')"
      >
        <Download class="h-3.5 w-3.5" />
        下载全部
      </Button>
    </div>

    <div v-if="!props.chapter" class="flex flex-1 items-center justify-center p-6 text-center text-sm text-muted-foreground">
      在左侧表格中选择一章查看
    </div>
    <template v-else>
      <!-- 标题区 -->
      <div class="shrink-0 px-4 pb-2 pt-3">
        <div class="flex flex-wrap items-center gap-2">
          <h3 class="text-lg font-semibold leading-tight">
            第{{ props.chapter.numStr || String(props.chapter.seq).padStart(3, '0') }}章
            {{ props.chapter.title || '（无标题）' }}
          </h3>
          <Badge v-if="props.chapter.pending && !props.marked" variant="warning">待核对</Badge>
          <Badge v-if="props.marked" variant="success">已核对</Badge>
          <Badge v-if="props.chapter.adjusted" variant="outline">已调整</Badge>
        </div>
        <p class="mt-1 text-xs text-muted-foreground">
          {{ formatNumber(props.chapter.chars) }} 字<template v-if="props.fileName"> · {{ props.fileName }}</template>
        </p>
      </div>

      <div class="min-h-0 flex-1 space-y-3 overflow-y-auto pb-4">
        <!-- 核对事项（常驻，advisory 在前） -->
        <template v-if="props.matters.length">
          <Alert v-for="matter in props.matters" :key="matter.id" :variant="matter.advisory ? 'warning' : 'default'" class="text-xs">
            <AlertTriangle v-if="matter.advisory" class="h-4 w-4 shrink-0" />
            <Info v-else class="h-4 w-4 shrink-0" />
            <div>
              <p>{{ matter.text }}</p>
              <p v-if="matter.detail" class="mt-1 text-muted-foreground">{{ matter.detail }}</p>
            </div>
          </Alert>
        </template>

        <!-- 正文预览 / 处理说明 -->
        <div class="flex items-center gap-1 border-b" role="tablist" aria-label="章节详情标签">
          <button
            type="button"
            role="tab"
            class="wb-atab"
            :class="{ 'wb-atab-active': panelTab === 'body' }"
            :aria-selected="panelTab === 'body'"
            @click="panelTab = 'body'"
          >
            正文预览
          </button>
          <button
            type="button"
            role="tab"
            class="wb-atab"
            :class="{ 'wb-atab-active': panelTab === 'notes' }"
            :aria-selected="panelTab === 'notes'"
            @click="panelTab = 'notes'"
          >
            处理说明
          </button>
        </div>

        <div v-show="panelTab === 'body'">
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
        </div>

        <div v-show="panelTab === 'notes'" class="space-y-3">
          <!-- 处理说明（仅被调整过的章节） -->
          <div v-if="props.chapter.adjusted" class="space-y-1.5 rounded-md border p-3 text-xs">
            <div class="font-medium">处理说明</div>
            <div class="flex flex-wrap items-center gap-1.5">
              <span v-if="props.chapter.orig_numStr" class="text-muted-foreground">
                原第{{ props.chapter.orig_numStr }}章 → 第{{ String(props.chapter.final_num ?? '').padStart(3, '0') }}章
              </span>
              <span
                class="inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium"
                :class="confidenceClass(props.chapter.confidence)"
              >
                置信{{ confidenceLabel(props.chapter.confidence) }}
              </span>
            </div>
            <ul v-if="props.chapter.reasons.length" class="list-disc space-y-0.5 pl-4 text-muted-foreground">
              <li v-for="(reason, i) in props.chapter.reasons" :key="i">{{ reason }}</li>
            </ul>
          </div>
          <p v-else class="text-xs text-muted-foreground">
            该章节未触发调整，按原结构直接保留。
          </p>
        </div>
      </div>

      <!-- 操作行：上一章 / 标记已核对 / 下一章 -->
      <div class="flex shrink-0 items-center gap-2 border-t px-4 py-3">
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

<style scoped>
.wb-atab {
  padding: 0.4rem 0.75rem;
  font-size: 0.8125rem;
  color: var(--muted-foreground);
  border-bottom: 2px solid transparent;
}
.wb-atab:hover {
  color: var(--foreground);
}
.wb-atab-active {
  color: hsl(var(--primary));
  font-weight: 600;
  border-bottom-color: hsl(var(--primary));
}
</style>
