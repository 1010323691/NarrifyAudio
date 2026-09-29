<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from '@/components/ui/Button.vue'
import Switch from '@/components/ui/Switch.vue'
import { useSettingsStore } from '@/stores/settings'
import { useToast } from '@/components/ui/toast'
import { ListChecks, Loader2, X } from 'lucide-vue-next'
import type { ParseChecks } from '@/types'

/** 解析检查项弹窗：双栏分组（内容质量 / 角色与表达）+ 每项一句用途说明。
 *  开关只改本地草稿；「保存设置」落项目配置并提交到父级 checks 后关闭，
 *  取消 / 关闭丢弃草稿。生效口径：仅影响之后提交的任务——值随提交固化进
 *  每个任务的配置快照，已提交任务保持原设置。 */
const props = defineProps<{
  open: boolean
  checks: Record<keyof ParseChecks, boolean>
  /** 有在途解析时禁用开关（避免跑中改动的歧义）。 */
  busy: boolean
}>()
const emit = defineEmits<{
  (e: 'close'): void
}>()

const settings = useSettingsStore()
const { push: toast } = useToast()

interface CheckItem {
  key: keyof ParseChecks
  label: string
  hint: string
}

/** 分组与文案面向用户（技术名 chunk / instruct 不出现在视图里）。 */
const GROUPS: { title: string; items: CheckItem[] }[] = [
  {
    title: '内容质量',
    items: [
      {
        key: 'check_chunk_alignment',
        label: '内容完整性检查',
        hint: '发现明显缺失时，尝试重新解析。',
      },
      {
        key: 'revalidate_splits',
        label: '断句异常检查',
        hint: '检查疑似断句错误，重新解析相关条目。',
      },
      {
        key: 'check_long_paragraphs',
        label: '超长段落检查',
        hint: '使用 AI 按语义拆分超长文本。',
      },
    ],
  },
  {
    title: '角色与表达',
    items: [
      {
        key: 'check_boundary_speakers',
        label: '角色归属检查',
        hint: '复核分段交界处的说话人归属。',
      },
      {
        key: 'validate_instructs',
        label: '语音指导检查',
        hint: '修复缺失或过长的语音指导。',
      },
      {
        key: 'spot_check_enabled',
        label: '角色归属抽查',
        hint: '按设定比例复核说话人归属。',
      },
    ],
  },
]
const ALL_ITEMS = GROUPS.flatMap((g) => g.items)

// 草稿：开关只改这里；保存成功才提交到 props.checks（父级 reactive）并落项目配置。
const draft = ref<Record<keyof ParseChecks, boolean>>({ ...props.checks })
const saving = ref(false)

watch(
  () => props.open,
  (open) => {
    if (!open) return
    // 每次打开从父级 checks 重新快照——上一次未保存的草稿随关闭丢弃。
    draft.value = { ...props.checks }
    saving.value = false
  },
)

const enabledCount = computed(() => ALL_ITEMS.filter((i) => draft.value[i.key]).length)

async function save() {
  if (saving.value) return
  saving.value = true
  // 先落项目配置（下次进入页面的初值 + 后续提交任务的快照来源），成功后才提交本地。
  const ok = await settings.save({ generation: { ...draft.value } })
  saving.value = false
  if (!ok) {
    toast({ title: '设置保存失败，请重试', variant: 'destructive' })
    return
  }
  Object.assign(props.checks, draft.value)
  emit('close')
}
</script>

<template>
  <div
    v-if="open"
    class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
    role="presentation"
    @click.self="emit('close')"
  >
    <!-- 约 800px 宽；内部滚动区 + 固定底栏（底部操作始终可见） -->
    <div
      class="flex max-h-[85vh] w-full max-w-[800px] flex-col overflow-hidden rounded-xl border bg-background shadow-xl"
      role="dialog"
      aria-modal="true"
      aria-labelledby="parse-checks-title"
    >
      <div class="flex shrink-0 items-start justify-between gap-4 px-6 pt-5 pb-4">
        <div class="min-w-0">
          <h2 id="parse-checks-title" class="flex items-center gap-2 text-xl font-semibold">
            <ListChecks class="h-5 w-5 text-primary" />
            解析检查项
          </h2>
          <p class="mt-1 text-[13px] text-muted-foreground">检查内容完整性、角色归属与语音指导。</p>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          class="h-8 w-8 shrink-0 p-0"
          aria-label="关闭解析检查项窗口"
          @click="emit('close')"
        >
          <X class="h-4 w-4" />
        </Button>
      </div>

      <div class="min-h-0 flex-1 overflow-y-auto px-6 pb-5">
        <!-- 双栏分组；窄屏（<768px）回落单列 -->
        <div class="grid grid-cols-1 gap-x-8 gap-y-4 md:grid-cols-2">
          <section v-for="g in GROUPS" :key="g.title" class="rounded-lg border">
            <h3 class="border-b px-4 py-2.5 text-[15px] font-semibold">{{ g.title }}</h3>
            <ul class="px-4">
              <li
                v-for="(item, idx) in g.items"
                :key="item.key"
                class="flex items-center justify-between gap-4 py-3"
                :class="{ 'border-t': idx > 0 }"
              >
                <div class="min-w-0">
                  <p class="text-base">{{ item.label }}</p>
                  <p class="mt-0.5 text-[13px] text-muted-foreground">{{ item.hint }}</p>
                </div>
                <Switch
                  small
                  :model-value="draft[item.key]"
                  :disabled="busy || saving"
                  @update:model-value="draft[item.key] = $event"
                />
              </li>
            </ul>
          </section>
        </div>

        <p class="mt-3 text-[13px] text-muted-foreground">
          基础保障始终开启：JSON 格式重试、超长文本机械分段兜底。
        </p>
      </div>

      <!-- 固定底栏：已开启计数 + 生效说明（小号灰字）+ 取消（不保存）/ 保存设置（落配置后生效） -->
      <div class="flex shrink-0 flex-wrap items-center justify-between gap-3 border-t px-6 py-4">
        <div class="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
          <span class="text-[13px] text-muted-foreground">已开启 {{ enabledCount }} 项检查</span>
          <span class="text-xs text-muted-foreground">保存后仅对新提交的任务生效，已提交任务保持原设置。</span>
        </div>
        <div class="flex shrink-0 items-center gap-2">
          <Button variant="outline" size="sm" :disabled="saving" @click="emit('close')">取消</Button>
          <Button size="sm" :disabled="saving" @click="save()">
            <Loader2 v-if="saving" class="h-4 w-4 animate-spin" />
            保存设置
          </Button>
        </div>
      </div>
    </div>
  </div>
</template>
