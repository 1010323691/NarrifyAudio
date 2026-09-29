<script setup lang="ts">
import Alert from '@/components/ui/Alert.vue'
import Badge from '@/components/ui/Badge.vue'
import Button from '@/components/ui/Button.vue'
import { AlertTriangle, Check, Download, Info, RotateCcw } from 'lucide-vue-next'
import { formatNumber } from '@/utils/format'
import { confidenceClass, confidenceLabel } from '@/utils/bookLabels'
import type { WorkbenchChapter, WorkbenchMatter } from '@/api/textFormat'

const props = defineProps<{
  chapter: WorkbenchChapter | null
  matters: WorkbenchMatter[]
  fileName: string | null
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
  (e: 'download'): void
}>()
</script>

<template>
  <div class="flex h-full min-h-0 flex-col">
    <div v-if="!props.chapter" class="flex h-full items-center justify-center p-6 text-sm text-muted-foreground">
      在左侧表格中选择一章查看
    </div>
    <template v-else>
      <!-- header -->
      <div class="border-b px-4 py-3">
        <div class="flex flex-wrap items-center gap-2">
          <h3 class="text-sm font-semibold leading-tight">
            第{{ String(props.chapter.final_num ?? props.chapter.seq).padStart(3, '0') }}章
            {{ props.chapter.title || '（无标题）' }}
          </h3>
          <Badge v-if="props.chapter.pending && !props.marked" variant="warning">待核对</Badge>
          <Badge v-if="props.marked" variant="success">已核对</Badge>
          <Badge v-if="props.chapter.adjusted" variant="outline">已调整</Badge>
        </div>
        <p class="mt-1 text-xs text-muted-foreground">
          {{ formatNumber(props.chapter.chars) }} 字 · {{ props.fileName ?? '（无对应文件）' }}
        </p>
      </div>

      <div class="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
        <!-- 核对事项（advisory 在前） -->
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

        <p v-if="!props.matters.length && !props.chapter.adjusted" class="text-xs text-muted-foreground">
          该章节未触发调整，按原结构直接保留。
        </p>
      </div>

      <!-- 操作行 -->
      <div class="flex items-center gap-2 border-t px-4 py-3">
        <Button variant="outline" size="sm" class="shrink-0" :disabled="!props.hasPrev" @click="emit('prev')">
          上一章
        </Button>
        <Button
          size="sm"
          class="shrink-0"
          :disabled="props.marksBusy"
          :variant="props.marked ? 'outline' : 'default'"
          @click="emit('mark')"
        >
          <Check v-if="props.marked" class="h-4 w-4" />
          <RotateCcw v-else class="h-4 w-4" />
          {{ props.marked ? '撤销核对' : '标记已核对' }}
        </Button>
        <Button variant="outline" size="sm" class="shrink-0" :disabled="!props.hasNext" @click="emit('next')">
          下一章
        </Button>
        <Button
          variant="ghost"
          size="sm"
          class="ml-auto shrink-0"
          :disabled="!props.canRead"
          :title="props.canRead ? '下载本章' : '该版本正文不可用'"
          @click="emit('download')"
        >
          <Download class="h-4 w-4" />
          下载
        </Button>
      </div>
    </template>
  </div>
</template>
