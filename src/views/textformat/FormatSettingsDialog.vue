<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import Button from '@/components/ui/Button.vue'
import Switch from '@/components/ui/Switch.vue'
import { Info, Settings2, X } from 'lucide-vue-next'
import type { SplitMode } from '@/api/textFormat'
import type { TextToggles } from '@/types'

const props = defineProps<{
  open: boolean
  initial: TextToggles | null
  lengthTarget: number
  busy: boolean
  splitMode: SplitMode
}>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'save', draft: TextToggles): void
  (e: 'reprocess', draft: TextToggles, mode: SplitMode): void
}>()

/** 分册方式两选一（互斥）：按字数分册优先，否则智能分册（0 章节自动兜底按字数）。 */
const MODES: { value: SplitMode; label: string; desc: string }[] = [
  { value: 'smart', label: '智能分册', desc: '按章节识别并拆分' },
  { value: 'by_length', label: '按字数分册', desc: '按目标字数拆分' },
]
const modeHint = computed(() => {
  const target = `约 ${props.lengthTarget} 字/册`
  if (mode.value === 'by_length') return `按${target}拆分全文，切点落在段落/句子边界`
  return `未识别到章节时，自动按${target}拆分`
})
const mode = ref<SplitMode>('smart')

/** 「章节识别」并入分册方式：不再单独暴露。排版阶段对章节标题行的独立成段
 *  处理恒开（服务端落流程快照时固定 detect_chapters=true），两种分册方式下
 *  排版质量都不降级。 */
const fallbackToggles: TextToggles = {
  keep_single_space: false,
  sentence_break: true,
  dialogue_separate: true,
  detect_chapters: true,
  punct_ellipsis: true,
  punct_repeated: true,
  punct_lone_ascii: false,
  punct_quotes: false,
  punct_dash: false,
  live: false,
}

interface ToggleField {
  key: Exclude<keyof TextToggles, 'live' | 'detect_chapters'>
  label: string
  hint: string
}
/** 文本排版（高频）：段落与空格类规则。 */
const LAYOUT_FIELDS: ToggleField[] = [
  { key: 'sentence_break', label: '句末断行', hint: '按句号、问号、感叹号后换行' },
  { key: 'dialogue_separate', label: '对话分段', hint: '将引号内的对话独立成段' },
  { key: 'keep_single_space', label: '保留单空格', hint: '保留正文中的单个空格' },
]
/** 标点整理：高频与低频（原「高级清理」）规则同级平铺展示。 */
const PUNCT_FIELDS: ToggleField[] = [
  { key: 'punct_ellipsis', label: '省略号规范', hint: '连续句点统一为省略号' },
  { key: 'punct_repeated', label: '连续标点合并', hint: '重复的同类标点仅保留一个' },
  { key: 'punct_lone_ascii', label: '清理孤立西文标点', hint: '中文旁的半角逗号/叹号/问号转全角' },
  { key: 'punct_quotes', label: '引号规范', hint: '统一引号配对与方向' },
  { key: 'punct_dash', label: '破折号规范', hint: '统一破折号写法' },
]
const ALL_FIELDS = [...LAYOUT_FIELDS, ...PUNCT_FIELDS]

const draft = ref<TextToggles | null>(null)
const saving = ref(false)
const panel = ref<HTMLElement | null>(null)
let returnFocus: HTMLElement | null = null

watch(
  () => props.open,
  (open) => {
    if (open) {
      draft.value = { ...(props.initial ?? fallbackToggles) }
      mode.value = props.splitMode
      returnFocus = document.activeElement as HTMLElement | null
    } else {
      saving.value = false
    }
  },
)
watch(
  () => props.initial,
  (value) => {
    if (props.open && value) draft.value = { ...value }
  },
)

const dirty = computed(() => {
  if (!draft.value) return false
  const a = draft.value as unknown as Record<string, unknown>
  const b = (props.initial ?? fallbackToggles) as unknown as Record<string, unknown>
  return ALL_FIELDS.some((f) => a[f.key] !== b[f.key]) || mode.value !== props.splitMode
})

/** 落盘前固定 detect_chapters=true（章节识别恒开，见上方注释）。 */
function finalDraft(): TextToggles | null {
  if (!draft.value) return null
  return { ...draft.value, detect_chapters: true }
}

async function saveDraft() {
  const value = finalDraft()
  if (!value || saving.value) return
  saving.value = true
  emit('save', value)
  saving.value = false
}

function saveAndReprocess() {
  const value = finalDraft()
  if (!value || props.busy) return
  emit('reprocess', value, mode.value)
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    emit('close')
    return
  }
  if (event.key !== 'Tab' || !panel.value) return
  const focusable = [...panel.value.querySelectorAll<HTMLElement>(
    'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )]
  if (!focusable.length) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

onBeforeUnmount(() => {
  returnFocus?.focus()
})
</script>

<template>
  <Teleport to="body">
    <div v-if="props.open" class="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div class="absolute inset-0 bg-black/50" @click="emit('close')" />
      <!-- 固定标题/底部 + 中间滚动：弹窗内容再多也不顶出视口。 -->
      <section
        ref="panel"
        role="dialog"
        aria-modal="true"
        aria-label="处理设置"
        class="relative flex max-h-[85vh] w-full max-w-2xl flex-col rounded-xl border bg-background shadow-lg outline-none"
        @keydown="onKeydown"
      >
        <header class="flex shrink-0 items-center gap-2 border-b px-5 py-3.5">
          <Settings2 class="h-4 w-4" />
          <div class="min-w-0">
            <h2 class="text-sm font-semibold">处理设置</h2>
            <p class="text-xs text-muted-foreground">调整分册方式与文本整理规则</p>
          </div>
          <button
            type="button"
            class="ml-auto rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            aria-label="关闭"
            @click="emit('close')"
          >
            <X class="h-4 w-4" />
          </button>
        </header>

        <div class="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-4">
          <!-- 分册方式：三个互斥选项置顶，替代原「整本处理」独立开关。 -->
          <div>
            <h3 class="mb-2 text-sm font-medium">分册方式</h3>
            <div role="radiogroup" aria-label="分册方式" class="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <button
                v-for="m in MODES"
                :key="m.value"
                type="button"
                role="radio"
                :aria-checked="mode === m.value"
                :disabled="props.busy"
                class="flex items-start gap-2.5 rounded-lg border p-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
                :class="mode === m.value
                  ? 'border-primary/50 bg-primary/5'
                  : 'border-input bg-background hover:border-primary/30'"
                @click="mode = m.value"
              >
                <span
                  class="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border-2"
                  :class="mode === m.value ? 'border-primary' : 'border-muted-foreground/40'"
                  aria-hidden="true"
                >
                  <span v-if="mode === m.value" class="h-2 w-2 rounded-full bg-primary" />
                </span>
                <span class="min-w-0">
                  <span class="block text-sm font-medium">{{ m.label }}</span>
                  <span class="block text-xs text-muted-foreground">{{ m.desc }}</span>
                </span>
              </button>
            </div>
            <p class="mt-2 flex items-start gap-1.5 text-xs text-muted-foreground">
              <Info class="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                {{ modeHint }}（管理员配置）
              </span>
            </p>
          </div>

          <!-- 两组设置：桌面双列、窄屏单列。 -->
          <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
            <div class="rounded-lg border p-4">
              <h3 class="text-sm font-medium">文本排版</h3>
              <p class="mt-0.5 text-xs text-muted-foreground">整理正文的空格与段落。</p>
              <label v-for="field in LAYOUT_FIELDS" :key="field.key" class="mt-3 flex items-start justify-between gap-3">
                <span class="min-w-0">
                  <span class="block text-sm">{{ field.label }}</span>
                  <span class="block text-xs text-muted-foreground">{{ field.hint }}</span>
                </span>
                <Switch
                  :model-value="!!(draft && draft[field.key])"
                  :disabled="props.busy"
                  @update:model-value="(v: boolean) => { if (draft) (draft as any)[field.key] = v }"
                />
              </label>
              <p class="mt-4 flex items-start gap-1.5 border-t pt-3 text-xs text-muted-foreground">
                <Info class="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>章节标题行恒按独立段落排版；分册阶段自动识别章节。</span>
              </p>
            </div>

            <div class="rounded-lg border p-4">
              <h3 class="text-sm font-medium">标点整理</h3>
              <p class="mt-0.5 text-xs text-muted-foreground">统一常见标点写法。</p>
              <label v-for="field in PUNCT_FIELDS" :key="field.key" class="mt-3 flex items-start justify-between gap-3">
                <span class="min-w-0">
                  <span class="block text-sm">{{ field.label }}</span>
                  <span class="block text-xs text-muted-foreground">{{ field.hint }}</span>
                </span>
                <Switch
                  :model-value="!!(draft && draft[field.key])"
                  :disabled="props.busy"
                  @update:model-value="(v: boolean) => { if (draft) (draft as any)[field.key] = v }"
                />
              </label>
            </div>
          </div>
      </div>

      <footer class="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-t px-5 py-3">
        <span class="text-xs text-muted-foreground">仅对本项目生效。</span>
        <span class="text-xs text-muted-foreground">保存仅更新设置，当前结果不会改变；若要立即应用，请选择「保存并重新处理」。</span>
        <div class="ml-auto flex items-center gap-2">
          <Button variant="ghost" size="sm" @click="emit('close')">取消</Button>
          <Button variant="outline" size="sm" class="text-primary" :disabled="props.busy" @click="saveAndReprocess">
            {{ props.busy ? '处理中，暂时不能重新处理' : '保存并重新处理' }}
          </Button>
          <Button size="sm" :disabled="props.busy || !dirty || saving" @click="saveDraft">
            保存设置
          </Button>
        </div>
      </footer>
      </section>
    </div>
  </Teleport>
</template>
