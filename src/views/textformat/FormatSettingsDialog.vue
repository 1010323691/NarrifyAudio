<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import Button from '@/components/ui/Button.vue'
import Switch from '@/components/ui/Switch.vue'
import { Settings2 } from 'lucide-vue-next'
import type { TextToggles } from '@/types'

const props = defineProps<{
  open: boolean
  initial: TextToggles | null
  lengthTarget: number
  busy: boolean
  wholeBook: boolean
}>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'save', draft: TextToggles): void
  (e: 'reprocess', draft: TextToggles, wholeBook: boolean): void
}>()

interface ToggleField {
  key: Exclude<keyof TextToggles, 'live'>
  label: string
  hint: string
}
const FIELDS: ToggleField[] = [
  { key: 'keep_single_space', label: '保留单空格', hint: '不折叠正文中的单个空格' },
  { key: 'sentence_break', label: '句末断行', hint: '句号/问号/感叹号后换行' },
  { key: 'dialogue_separate', label: '对话分段', hint: '「」/“”对话独立成段' },
  { key: 'detect_chapters', label: '章节识别', hint: '按「第N章」等格式识别章节结构' },
  { key: 'punct_ellipsis', label: '省略号规范', hint: '连续句点合并为 ……' },
  { key: 'punct_repeated', label: '连续标点合并', hint: '重复的同类标点只保留一个' },
  { key: 'punct_lone_ascii', label: '清理孤立西文标点', hint: '删除无配对的英文括号/引号' },
  { key: 'punct_quotes', label: '引号规范', hint: '统一引号配对与方向' },
  { key: 'punct_dash', label: '破折号规范', hint: '统一破折号写法' },
]

const draft = ref<TextToggles | null>(null)
const wholeBook = ref(false)
const saving = ref(false)
const panel = ref<HTMLElement | null>(null)
let returnFocus: HTMLElement | null = null

watch(
  () => props.open,
  (open) => {
    if (open) {
      draft.value = { ...(props.initial ?? fallbackToggles) }
      wholeBook.value = props.wholeBook
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

const dirty = computed(() => {
  if (!draft.value) return false
  const a = draft.value as unknown as Record<string, unknown>
  const b = (props.initial ?? fallbackToggles) as unknown as Record<string, unknown>
  return FIELDS.some((f) => a[f.key] !== b[f.key]) || wholeBook.value !== props.wholeBook
})

async function saveDraft() {
  if (!draft.value || saving.value) return
  saving.value = true
  emit('save', { ...draft.value })
  saving.value = false
}

function reprocess() {
  if (!draft.value || props.busy) return
  emit('reprocess', { ...draft.value }, wholeBook.value)
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
      <section
        ref="panel"
        role="dialog"
        aria-modal="true"
        aria-label="处理设置"
        class="relative max-h-full w-full max-w-lg overflow-y-auto rounded-xl border bg-background shadow-lg outline-none"
        @keydown="onKeydown"
      >
        <header class="flex items-center gap-2 border-b px-4 py-3">
          <Settings2 class="h-4 w-4" />
          <h2 class="text-sm font-semibold">处理设置</h2>
          <span class="ml-auto text-xs text-muted-foreground">保存后仅在新一次处理中生效</span>
        </header>

        <div class="space-y-3 px-4 py-3">
          <label v-for="field in FIELDS" :key="field.key" class="flex items-start justify-between gap-3">
            <span class="min-w-0">
              <span class="block text-sm">{{ field.label }}</span>
              <span class="block text-xs text-muted-foreground">{{ field.hint }}</span>
            </span>
            <Switch :model-value="!!(draft && draft[field.key])" :disabled="props.busy" @update:model-value="(v: boolean) => { if (draft) (draft as any)[field.key] = v }" />
          </label>

          <div class="rounded-md border bg-secondary/40 p-3 text-xs">
            <p class="font-medium">整本处理</p>
            <p class="mt-0.5 text-muted-foreground">
              开启后不做章节拆分，整本书作为单一文件输出（跳过智能识别）。
            </p>
            <div class="mt-2 flex items-center gap-2">
              <Switch :model-value="wholeBook" :disabled="props.busy" @update:model-value="(v: boolean) => { wholeBook = v }" />
              <span class="text-muted-foreground">
                分册目标字数：约 {{ props.lengthTarget }} 字/册（当前生效值，仅按字数分册时使用）
              </span>
            </div>
          </div>
        </div>

        <footer class="flex items-center gap-2 border-t px-4 py-3">
          <Button variant="outline" size="sm" :disabled="props.busy || !dirty || saving" @click="saveDraft">
            保存设置
          </Button>
          <Button size="sm" :disabled="props.busy || !draft" @click="reprocess">
            {{ props.busy ? '处理中，暂时不能重新处理' : '按这些设置重新处理' }}
          </Button>
          <Button variant="ghost" size="sm" class="ml-auto" @click="emit('close')">关闭</Button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>
