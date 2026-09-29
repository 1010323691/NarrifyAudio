<script setup lang="ts">
import { computed, ref } from 'vue'
import Alert from '@/components/ui/Alert.vue'
import { Loader2, AlertTriangle, ScanText, Maximize2, Shrink } from 'lucide-vue-next'
import { distinctSpeakerHues } from '@/utils/speakerColor'
import { padChapterNum } from '@/utils/bookLabels'
import {
  TONE_CHIP,
  TONE_TEXT,
  type ParseRow,
  type ResultPreview,
  type SourcePreview,
} from '@/composables/useScriptParseWorkbench'

const props = defineProps<{
  row: ParseRow | null
  tab: 'result' | 'source'
  resultPreview: ResultPreview
  sourcePreview: SourcePreview
  numPad?: number
}>()
const emit = defineEmits<{
  (e: 'update:tab', tab: 'result' | 'source'): void
}>()

/** 「放大阅读」（原文 tab）：纯显示偏好——字号 16→18、行距 1.8→1.9，不改源文本。 */
const enlarged = ref(false)

const chapterLabel = computed(() =>
  props.row ? padChapterNum(props.row.chapter.numStr, props.numPad ?? 3) : '',
)

/** 结果 tab 的角色图例：按归属文本段数降序（段数多者靠前）；段数相同保持首次出现顺序
 *  （sort 稳定）。顺序同时作为色环均分的分配序（见 distinctSpeakerHues）。 */
const speakers = computed(() => {
  const order: string[] = []
  const count: Record<string, number> = {}
  for (const e of props.resultPreview.entries) {
    if (!(e.speaker in count)) order.push(e.speaker)
    count[e.speaker] = (count[e.speaker] ?? 0) + 1
  }
  return order.sort((a, b) => count[b] - count[a])
})
const speakerName = (s: string) => (s === 'NARRATOR' ? '旁白' : s)
const characterCount = computed(() => new Set(props.resultPreview.entries.map((e) => e.speaker)).size)

/** 章节内角色色相：色环均分（见 distinctSpeakerHues——N 个角色色差恒 360/N，互不相近）；
 *  NARRATOR 不占色环、消费方统一中性灰。 */
const speakerHueMap = computed(() => distinctSpeakerHues(speakers.value))
const tint = (s: string) =>
  s === 'NARRATOR' ? {} : ({ '--spk-hue': String(speakerHueMap.value[s] ?? 0) })

/** 失败/超时/取消但仍有旧结果 → 结果 tab 顶部提示「展示的是上一次结果」。 */
const showingStaleResult = computed(
  () =>
    !!props.row?.chapter.result &&
    (props.row.status === 'failed' || props.row.status === 'timeout' || props.row.status === 'cancelled'),
)

/** 无结果时的空态文案（随行状态区分：在途 / 待解析 / 结果不可用）。 */
const emptyHint = computed(() => {
  if (!props.row) return '点击左侧章节，查看解析结果与原文。'
  switch (props.row.status) {
    case 'active':
      return `本章正在${props.row.label}（${Math.round(props.row.progress * 100)}%），完成后结果会显示在这里。`
    case 'stale':
      return '分册文本已变更，旧结果不可用——勾选本章重新解析后会显示在这里。'
    default:
      return '本章尚未解析，在左表勾选后点击底部「开始解析」。'
  }
})
</script>

<template>
  <div class="flex h-full min-h-0 flex-col">
    <!-- 页头：大号章号 + 右侧「圆点 + 文字」状态；
         44px（h-11）压缩档：章号 leading-none，把高度让给正文；
         页头本身不画分割线，分割只由 tab 行的下划线行提供。 -->
    <header class="flex h-11 shrink-0 items-center gap-3 px-4">
      <div class="flex min-w-0 flex-1 items-baseline gap-1.5">
        <!-- 未选择态用小号灰字（与排版页详情面板一致），选中态用大号章号。 -->
        <span
          class="shrink-0 leading-none tracking-tight"
          :class="row
            ? 'text-[22px] font-bold'
            : 'font-normal text-sm text-muted-foreground/60'"
        >
          <template v-if="row">第{{ chapterLabel }}章</template>
          <template v-else>未选择</template>
        </span>
        <span v-if="row && row.chapter.title" class="min-w-0 truncate text-sm text-muted-foreground">
          · {{ row.chapter.title }}
        </span>
      </div>
      <span v-if="row" class="flex shrink-0 items-center gap-1.5" :class="TONE_TEXT[row.tone]">
        <span class="h-1.5 w-1.5 shrink-0 rounded-full" :class="TONE_CHIP[row.tone].dot" aria-hidden="true" />
        <span class="text-xs font-medium">{{ row.label }}</span>
      </span>
    </header>

    <!-- tab 行：下划线式（激活 = 身份色 + 2px 下划线），顺序 解析结果 | 原文（参考稿口径）；
         右侧「放大阅读」切换（仅原文 tab）：纯显示偏好，源文本不动。 -->
    <nav class="flex h-10 shrink-0 items-stretch gap-5 border-b px-4" aria-label="章节预览切换">
      <button
        v-for="t in [['result', '解析结果'], ['source', '原文']] as const"
        :key="t[0]"
        type="button"
        class="wb-tab"
        :class="{ 'wb-tab-active': tab === t[0] }"
        @click="emit('update:tab', t[0])"
      >
        {{ t[1] }}
      </button>
      <button
        v-if="tab === 'source'"
        type="button"
        class="ml-auto flex items-center gap-1 self-center text-xs text-muted-foreground transition-colors hover:text-foreground"
        :class="{ 'font-medium text-primary': enlarged }"
        :aria-pressed="enlarged"
        @click="enlarged = !enlarged"
      >
        <Maximize2 v-if="!enlarged" class="h-3.5 w-3.5" />
        <Shrink v-else class="h-3.5 w-3.5" />
        放大阅读
      </button>
    </nav>

    <div class="min-h-0 flex-1 overflow-y-auto">
      <!-- 解析结果 -->
      <!-- 未选择时容器占满滚动区高度（h-full 需父级有确定高度，滚动容器才有），空态才能垂直居中。 -->
      <div v-if="tab === 'result'" :key="row?.chapter.name ?? 'none'" class="panel-enter p-5" :class="{ 'h-full': !row }">
        <div v-if="!row" class="flex h-full items-center justify-center text-sm text-muted-foreground">
          <ScanText class="mr-2 h-4 w-4" />
          点击左侧章节，查看解析结果与原文。
        </div>
        <template v-else>
          <Alert v-if="showingStaleResult" variant="warning">
            <AlertTriangle class="h-4 w-4 shrink-0" />
            本次解析未成功（{{ row.label }}），展示的是上一次的结果。
          </Alert>

          <div v-if="resultPreview.status === 'loading'" class="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 class="h-4 w-4 animate-spin" />解析结果加载中…
          </div>
          <Alert v-else-if="resultPreview.status === 'error'" variant="destructive">
            <AlertTriangle class="h-4 w-4 shrink-0" />
            {{ resultPreview.error }}
          </Alert>
          <div
            v-else-if="resultPreview.entries.length === 0"
            class="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground"
          >
            {{ emptyHint }}
          </div>
          <template v-else>
            <p class="text-xs tabular-nums text-muted-foreground">
              {{ resultPreview.entries.length }} 段文本 · {{ characterCount }} 个角色
            </p>
            <!-- 角色图例：小圆点 + 角色色纯文字（无 chip 底），点色随 currentColor 分档。 -->
            <div class="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1.5">
              <span
                v-for="s in speakers"
                :key="s"
                class="spk-legend inline-flex items-center gap-1.5 text-xs font-medium"
                :class="{ 'spk-neutral': s === 'NARRATOR' }"
                :style="tint(s)"
                :title="speakerName(s)"
              >
                <span class="h-1.5 w-1.5 shrink-0 rounded-full" style="background: currentColor" aria-hidden="true" />
                {{ speakerName(s) }}
              </span>
            </div>
            <!-- 条目：两位序号 + 角色色名字 + 左彩条引言块（编辑式排版，无边框卡片）。 -->
            <ol class="mt-5 space-y-5">
              <li
                v-for="(entry, i) in resultPreview.entries"
                :key="i"
                class="spk-entry flex gap-3"
                :class="{ 'spk-neutral': entry.speaker === 'NARRATOR' }"
                :style="tint(entry.speaker)"
              >
                <span class="w-7 shrink-0 text-right text-[13px] tabular-nums text-muted-foreground/70">
                  {{ String(i + 1).padStart(2, '0') }}
                </span>
                <span class="min-w-0 flex-1">
                  <span class="spk-name block pr-2 text-[13px] font-medium" :title="entry.instruct || undefined">
                    {{ speakerName(entry.speaker) }}
                  </span>
                  <p class="spk-quote mt-2 text-[15px] leading-[1.8]">{{ entry.text }}</p>
                </span>
              </li>
            </ol>
          </template>
        </template>
      </div>

      <!-- 原文：无边框阅读页（与解析结果同面板底色，不再套卡片）。
           保真约束：pre 原样渲染 sourcePreview.text——段落、标点、空行、
           「第01章」等原文字样一律不动；空行不压缩（压缩属独立的阅读排版选项）。
           边距：左右 24px / 顶部 20px / 底部 32px；字号 16px 常规字重，行距 1.8。 -->
      <div v-else :key="row?.chapter.name ?? 'none'" class="panel-enter px-6 pt-5 pb-8" :class="{ 'h-full': !row }">
        <div v-if="!row" class="flex h-full items-center justify-center text-sm text-muted-foreground">
          点击左侧章节，查看分册原文。
        </div>
        <div v-else-if="sourcePreview.status === 'loading'" class="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 class="h-4 w-4 animate-spin" />原文加载中…
        </div>
        <Alert v-else-if="sourcePreview.status === 'error'" variant="destructive">
          <AlertTriangle class="h-4 w-4 shrink-0" />
          {{ sourcePreview.error }}
        </Alert>
        <pre
          v-else
          class="whitespace-pre-wrap break-words font-sans font-normal"
          :class="enlarged ? 'text-lg leading-[1.9]' : 'text-base leading-[1.8]'"
        >{{ sourcePreview.text }}</pre>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 下划线 tab：激活 = 身份色文字 + 2px 下划线（bottom -1px 盖住行 1px 分割线）。 */
.wb-tab {
  position: relative;
  font-size: 0.8125rem;
  color: var(--muted-foreground);
  transition: color 150ms ease;
}
.wb-tab:hover {
  color: var(--foreground);
}
.wb-tab-active,
.wb-tab-active:hover {
  color: hsl(var(--primary));
  font-weight: 600;
}
.wb-tab-active::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: -1px;
  height: 2px;
  border-radius: 1px;
  background: hsl(var(--primary));
}
/* 角色色（高饱和冲击档）：--spk-hue 由章节内均分色环表（distinctSpeakerHues）挂到承载节点（图例 / 名字 / 引言块）。
   角色 = 高饱和（85%）强对比；暗色档提亮保证深色底可读。
   原 speakerColor 高饱和档为 ChapterPreview 保留，勿在此回退。 */
.spk-legend,
.spk-name {
  color: hsl(var(--spk-hue) 85% 45%);
}
.dark .spk-legend,
.dark .spk-name {
  color: hsl(var(--spk-hue) 85% 65%);
}
.spk-quote {
  border-left: 3px solid hsl(var(--spk-hue) 85% 48%);
  padding-left: 0.875rem;
  white-space: pre-wrap;
  word-break: break-words;
}
.dark .spk-quote {
  border-left-color: hsl(var(--spk-hue) 85% 58%);
}
/* 旁白 = 中性灰：旁白是叙述者不是角色，不与角色抢颜色（角色色留给角色的高饱和冲击）。
   规则放最后，specificity 压过上面的 .dark 档。 */
.spk-legend.spk-neutral,
.spk-entry.spk-neutral .spk-name {
  color: hsl(var(--muted-foreground));
}
.spk-entry.spk-neutral .spk-quote {
  border-left-color: hsl(var(--border));
}
/* 切章/切 tab 时内容容器随 :key 重挂载播一次；keep-alive 再激活 DOM 保留不重播；
   reduced-motion 由 style.css 全局兜底。 */
@keyframes panel-in {
  from {
    opacity: 0;
    transform: translateY(2px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
.panel-enter {
  animation: panel-in 180ms ease-out;
}
</style>
