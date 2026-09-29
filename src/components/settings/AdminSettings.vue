<script setup lang="ts">
// 管理端「平台功能配置」编辑区（S9：自 views/Settings.vue 拆出）。
// 走 settings store 的 root 通道（loadRoot/saveRoot）——与项目级 config
// 通道互不写入（Q10）。父级（Admin.vue）以 section 属性切换 text/models/audio。
import { onMounted, ref, watch } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useToast } from '@/components/ui/toast'
import { listLlmModels } from '@/api/admin'
import type { AppConfig, TextToggles } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardContent from '@/components/ui/CardContent.vue'
import Input from '@/components/ui/Input.vue'
import Label from '@/components/ui/Label.vue'
import Select from '@/components/ui/Select.vue'
import Textarea from '@/components/ui/Textarea.vue'
import Switch from '@/components/ui/Switch.vue'
import Alert from '@/components/ui/Alert.vue'
import {
  Save,
  Type,
  Server,
  SlidersHorizontal,
  MessageSquareText,
  AudioWaveform,
  AudioLines,
  Music4,
  Scissors,
  RefreshCw,
} from 'lucide-vue-next'

const props = withDefaults(defineProps<{ section?: 'text' | 'models' | 'audio' }>(), { section: 'text' })

const settings = useSettingsStore()
const { push: toast } = useToast()

const draft = ref<AppConfig | null>(null)
const originalPrompts = ref<AppConfig['prompts'] | null>(null)
const saving = ref(false)
const active = ref<'text' | 'models' | 'audio'>('text')
// 「拉取模型」：草稿值探测（无需先保存），结果下拉点选即填入模型名称。
const fetchingModels = ref(false)
const fetchedModels = ref<string[]>([])
const pickedModel = ref('')
const modelsTried = ref(false)

watch(() => props.section, (value) => {
  active.value = value
})

// detect_chapters 已并入章节核对工作台的「分册方式」（服务端落快照时恒开）、
// live 的自动重跑语义已移除——两者不再受平台默认控制，故不提供开关。
const TOGGLES: { key: keyof TextToggles; label: string }[] = [
  { key: 'sentence_break', label: '断句换段' },
  { key: 'dialogue_separate', label: '对话独立成段' },
  { key: 'keep_single_space', label: '保留单个空格' },
  { key: 'punct_ellipsis', label: '省略号统一' },
  { key: 'punct_repeated', label: '合并重复标点' },
  { key: 'punct_quotes', label: '引号成对' },
  { key: 'punct_lone_ascii', label: '半角标点转全角' },
  { key: 'punct_dash', label: '破折号统一' },
]

// 解析内 6 个检查开关是用户「文本解析」页的专属设置（随任务提交、固化进任务配置快照）：
// 管理台不再提供 UI，保存时也不得把它们持久化成平台默认——否则平台默认值会在
// get_config 的合并中压过项目配置里存的「上次选择」（与 backend/api/admin.py 的
// _USER_OWNED_CHECKS 剔除是双保险；顺带清掉历史平台默认里的旧值）。
const USER_OWNED_CHECKS = [
  'check_chunk_alignment',
  'check_boundary_speakers',
  'validate_instructs',
  'revalidate_splits',
  'check_long_paragraphs',
  'spot_check_enabled',
] as const

onMounted(async () => {
  active.value = props.section
  // Root (admin) channel — the store keeps it apart from the project config
  // store field, so saving below can never overwrite the project values.
  await settings.loadRoot()
  draft.value = settings.rootConfig ? JSON.parse(JSON.stringify(settings.rootConfig)) : null
  originalPrompts.value = draft.value ? { ...draft.value.prompts } : null
})

async function save() {
  if (!draft.value) return
  saving.value = true
  let ok = false
  const config = draft.value
  const seedValue = config.tts.batch_seed
  const seed = typeof seedValue === 'number' && Number.isFinite(seedValue) ? seedValue : -1
  const tts = {
    ...config.tts,
    batch_concurrency: Math.max(1, Math.min(128, Math.trunc(Number(config.tts.batch_concurrency) || 80))),
    batch_seed: Math.max(-1, Math.min(2147483647, Math.trunc(seed))),
  }
  const generation: Record<string, unknown> = {
    ...config.generation,
    parse_worker_concurrency: Math.max(1, Math.min(32, Math.trunc(Number(config.generation.parse_worker_concurrency) || 1))),
  }
  // 用户解析页专属的 6 个检查开关不落平台默认（见上方 USER_OWNED_CHECKS 注释）。
  for (const key of USER_OWNED_CHECKS) delete generation[key]
  const split = {
    length_target: Math.max(100, Math.min(200000, Math.trunc(Number(config.split?.length_target) || 3000))),
  }
  try {
    const patch: Record<string, unknown> = {
      text: config.text,
      audio: config.audio,
      tts,
      llm: config.llm,
      persona_prompts: config.persona_prompts,
      generation,
      ffmpeg: config.ffmpeg,
      bgm: config.bgm,
      split,
    }
    // The API echoes bundled defaults for display. Persist prompts only when
    // edited so saving unrelated settings does not freeze today's defaults.
    if (!originalPrompts.value
      || config.prompts.system_prompt !== originalPrompts.value.system_prompt
      || config.prompts.user_prompt !== originalPrompts.value.user_prompt) {
      patch.prompts = config.prompts
    }
    ok = await settings.saveRoot(patch)
    // Server-echoed values win for the sent sections (Q10: root channel
    // only — the project store's `config` is deliberately not written).
    if (ok && settings.rootConfig) {
      draft.value = JSON.parse(JSON.stringify(settings.rootConfig))
      originalPrompts.value = { ...settings.rootConfig.prompts }
    }
  } catch {
    ok = false
  }
  saving.value = false
  if (ok) toast({ title: '设置已保存', variant: 'success', description: '平台功能配置已更新，新启动的任务会使用新配置。' })
  else toast({ title: '保存失败', variant: 'destructive' })
}

// 用表单当前草稿值探测（未保存也能拉）；地址为空时只提示不发请求。
async function fetchModels() {
  if (!draft.value || fetchingModels.value) return
  const baseUrl = draft.value.llm.base_url.trim()
  if (!baseUrl) {
    toast({ title: '请先填写 API 地址', variant: 'destructive', description: '填好 LLM 服务地址后再拉取模型列表。' })
    return
  }
  fetchingModels.value = true
  fetchedModels.value = []
  modelsTried.value = false
  try {
    const result = await listLlmModels({ base_url: baseUrl, api_key: draft.value.llm.api_key })
    fetchedModels.value = result.models
    // 当前模型名若在新列表里，回填下拉选中项（watch 不 immediate，须手动同步）。
    if (result.models.length && result.models.includes(draft.value.llm.model_name)) {
      pickedModel.value = draft.value.llm.model_name
    }
    if (result.models.length) {
      toast({ title: `已获取 ${result.models.length} 个模型`, variant: 'success' })
    } else {
      toast({ title: '未获取到模型', variant: 'destructive', description: '该服务返回了空列表。' })
    }
  } catch (cause: any) {
    toast({ title: '拉取模型失败', variant: 'destructive', description: cause?.message || String(cause) })
  } finally {
    fetchingModels.value = false
    modelsTried.value = true
  }
}

function onPickModel(value: string | number) {
  const name = String(value)
  if (!name || !draft.value) return
  pickedModel.value = name
  draft.value.llm.model_name = name
}

// 手动输入与下拉保持一致：名字在列表里则选中；不在列表里则清空选中（避免残留旧值）。
watch(
  () => draft.value?.llm.model_name,
  (name) => {
    if (name && fetchedModels.value.includes(name)) pickedModel.value = name
    else if (fetchedModels.value.length) pickedModel.value = ''
  },
)
</script>

<template>
  <div class="admin-settings">
    <div class="settings-actions">
      <Button @click="save" :disabled="saving || !draft">
        <Save class="h-4 w-4" />{{ saving ? '保存中…' : '保存设置' }}
      </Button>
    </div>

    <Alert v-if="!draft" variant="destructive">平台功能配置暂时无法读取，请刷新后重试。</Alert>

    <template v-else>
      <!-- 文本排版 -->
      <Card v-if="active === 'text'">
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Type class="h-5 w-5" />文本排版</CardTitle>
        </CardHeader>
        <CardContent>
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div v-for="t in TOGGLES" :key="t.key" class="flex items-center justify-between">
              <Label class="font-normal">{{ t.label }}</Label>
              <Switch v-model="draft.text[t.key]" />
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- 分册（零章节兜底） -->
      <Card v-if="active === 'text'">
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Scissors class="h-5 w-5" />分册</CardTitle>
          <CardDescription>未识别出章节时的兜底拆分参数。</CardDescription>
        </CardHeader>
        <CardContent class="space-y-3">
          <div class="space-y-1.5">
            <Label for="split-length-target">按字数分册目标字数</Label>
            <div class="flex flex-wrap items-center gap-3">
              <Input id="split-length-target" v-model.number="draft.split.length_target" type="number" min="100" max="200000" step="50" class="max-w-[120px]" />
              <span class="text-xs text-muted-foreground">
                未识别出章节时，按字数平均分册的每册目标字数（100~200000，默认 3000）。新启动的分册任务生效。
              </span>
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- LLM 配置 -->
      <Card v-if="active === 'models'">
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Server class="h-5 w-5" />LLM 配置</CardTitle>
          <CardDescription>
            配置 LLM 服务地址和模型。
          </CardDescription>
        </CardHeader>
        <CardContent class="space-y-3">
          <div class="space-y-1.5">
            <Label>API 地址</Label>
            <Input v-model="draft.llm.base_url" placeholder="http://localhost:11434/v1" />
          </div>
          <div class="grid gap-4 sm:grid-cols-2">
            <div class="space-y-1.5">
              <Label>API Key</Label>
              <Input v-model="draft.llm.api_key" placeholder="local" />
            </div>
            <div class="space-y-1.5">
              <Label>模型名称</Label>
              <div class="flex flex-wrap items-center gap-2">
                <Input v-model="draft.llm.model_name" placeholder="如 qwen3:14b（必填）" class="min-w-0 flex-1" />
                <Button variant="outline" size="sm" :disabled="fetchingModels" @click="fetchModels">
                  <RefreshCw class="h-4 w-4" :class="fetchingModels ? 'animate-spin' : ''" />
                  {{ fetchingModels ? '拉取中…' : '拉取模型' }}
                </Button>
              </div>
              <Select v-if="fetchedModels.length" :modelValue="pickedModel" class="mt-2" @update:modelValue="onPickModel">
                <option value="" disabled hidden></option>
                <option v-for="m in fetchedModels" :key="m" :value="m">{{ m }}</option>
              </Select>
              <p v-else-if="modelsTried" class="mt-1 text-xs text-muted-foreground">该服务没有返回任何模型。</p>
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- 生成参数 -->
      <Card v-if="active === 'models'">
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><SlidersHorizontal class="h-5 w-5" />生成参数</CardTitle>
          <CardDescription>调整文本解析的分段和采样参数。</CardDescription>
        </CardHeader>
        <CardContent class="space-y-3">
          <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <div class="space-y-1.5">
              <Label>分段大小（字）</Label>
              <Input v-model.number="draft.generation.chunk_size" type="number" min="1" />
            </div>
            <div class="space-y-1.5">
              <Label>最大返回（tokens）</Label>
              <Input v-model.number="draft.generation.max_tokens" type="number" min="1" />
            </div>
            <div class="space-y-1.5">
              <Label>温度</Label>
              <Input v-model.number="draft.generation.temperature" type="number" step="0.1" min="0" max="2" />
            </div>
            <div class="space-y-1.5">
              <Label>Top-P</Label>
              <Input v-model.number="draft.generation.top_p" type="number" step="0.05" min="0" max="1" />
            </div>
          </div>
          <div class="space-y-1.5">
            <Label>文本解析 LLM 并发数</Label>
            <div class="flex flex-wrap items-center gap-3">
              <Input v-model.number="draft.generation.parse_worker_concurrency" type="number" min="1" max="32" step="1" class="max-w-[8rem]" />
              <span class="text-xs text-muted-foreground">
                控制每个后台 Worker 进程内主解析的 LLM 同时请求数。解析工作槽位为该值的 2 倍、最多提前准备或等待该队列。多进程总并发为各进程限额之和。
              </span>
            </div>
          </div>
          <div class="space-y-1.5">
            <Label>角色基础信息生成并发数</Label>
            <div class="flex flex-wrap items-center gap-3">
              <Input v-model.number="draft.generation.max_concurrency" type="number" min="1" step="1" class="max-w-[8rem]" />
              <span class="text-xs text-muted-foreground">
                控制角色基础信息的并行生成。
              </span>
            </div>
          </div>
          <div class="space-y-1.5">
            <Label>归属抽样率（0 = 关闭）</Label>
            <div class="flex flex-wrap items-center gap-3">
              <Input v-model.number="draft.generation.spot_check_rate" type="number" step="0.01" min="0" max="0.5" class="max-w-[8rem]" />
              <span class="text-xs text-muted-foreground">
                设置解析结果的抽样复核比例；0 表示关闭。归属抽样的总开关在用户「文本解析」页。
              </span>
            </div>
          </div>
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div class="flex items-center justify-between">
              <Label class="font-normal">纯归属标签删除</Label>
              <Switch v-model="draft.generation.delete_saying_tags" />
            </div>
            <div class="flex items-center justify-between">
              <div class="flex flex-col">
                <Label class="font-normal">超长段落上限（字）</Label>
                <span class="text-xs text-muted-foreground">
                  任何条目最终不得超过此字数（机械分段兜底恒生效）。超长段落检查开关（只控制 LLM 语义重切）在用户「文本解析」页。
                </span>
              </div>
              <Input
                v-model.number="draft.generation.max_paragraph_chars"
                type="number" min="10" step="10" class="w-20"
              />
            </div>
            <div class="flex items-center justify-between">
              <Label class="font-normal">纯标点条目吸收</Label>
              <Switch v-model="draft.generation.absorb_punct_entries" />
            </div>
            <div class="flex items-center justify-between">
              <Label class="font-normal">同人段落合并</Label>
              <Switch v-model="draft.generation.merge_same_speaker" />
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- Prompt 配置 -->
      <Card v-if="active === 'models'">
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><MessageSquareText class="h-5 w-5" />Prompt 配置</CardTitle>
          <CardDescription>查看或修改文本解析 Prompt；留空使用默认值。</CardDescription>
        </CardHeader>
        <CardContent class="space-y-3">
          <div class="space-y-1.5">
            <Label>System Prompt</Label>
            <Textarea v-model="draft.prompts.system_prompt" rows="8" class="font-mono text-xs" />
          </div>
          <div class="space-y-1.5">
            <Label>User Prompt（模板，含 <code class="text-xs">context</code> / <code class="text-xs">chunk</code> 占位符）</Label>
            <Textarea v-model="draft.prompts.user_prompt" rows="8" class="font-mono text-xs" />
          </div>
        </CardContent>
      </Card>

      <Card v-if="active === 'audio'">
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><AudioWaveform class="h-5 w-5" />TTS 批处理</CardTitle>
          <CardDescription>按文本长度排序后组批，仅使用批内上限。</CardDescription>
        </CardHeader>
        <CardContent>
          <div class="grid gap-4 sm:grid-cols-2">
            <div class="space-y-1.5">
              <Label for="tts-batch-limit">批内并发上限</Label>
              <div class="flex flex-wrap items-center gap-3">
                <Input id="tts-batch-limit" v-model.number="draft.tts.batch_concurrency" type="number" min="1" max="128" step="1" class="max-w-[120px]" :disabled="draft.tts.batch_auto" />
                <label class="flex items-center gap-2 text-sm text-muted-foreground" title="按同批最长文本字数向上匹配已测安全档位">
                  <input v-model="draft.tts.batch_auto" type="checkbox" class="h-4 w-4 rounded border-input accent-primary" />
                  自动
                </label>
              </div>
            </div>
            <div class="space-y-1.5">
              <Label for="tts-batch-seed">合成 Seed</Label>
              <Input id="tts-batch-seed" v-model.number="draft.tts.batch_seed" type="number" min="-1" max="2147483647" step="1" required class="max-w-[180px]" />
              <p class="text-xs text-muted-foreground">-1 表示每次随机；设置为 0 或正整数后，可用相同 Seed 复现结果。</p>
            </div>
          </div>
          <p class="text-xs text-muted-foreground">自动模式按同批最长文本字数匹配安全批量档位；Seed 设为 -1 时随机取值。</p>
        </CardContent>
      </Card>

      <!-- 音频分集 -->
      <Card v-if="active === 'audio'">
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><AudioLines class="h-5 w-5" />音频分集</CardTitle>
        </CardHeader>
        <CardContent>
          <div class="grid gap-4 sm:grid-cols-2">
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">目标时长</Label>
              <Input v-model="draft.audio.target_duration" placeholder="10:00" class="max-w-[120px]" />
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">智能对齐</Label>
              <Switch v-model="draft.audio.smart_align" />
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">偏移容差</Label>
              <Input v-model.number="draft.audio.align_tolerance" type="number" min="5" max="30" class="max-w-[100px]" />
              <span class="text-xs text-muted-foreground">秒</span>
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">命名格式</Label>
              <Input v-model="draft.audio.naming_format" placeholder="书名 第 {} 集" class="max-w-[160px]" />
              <span class="text-xs text-muted-foreground">完整文件名，{} 为编号</span>
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">起始编号</Label>
              <Input v-model="draft.audio.start_number" class="max-w-[100px]" />
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- 背景音乐 -->
      <Card v-if="active === 'audio'">
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Music4 class="h-5 w-5" />背景音乐</CardTitle>
          <CardDescription>
            设置章节匹配和混音参数。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div class="grid gap-4 sm:grid-cols-2">
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">BGM 音量</Label>
              <Input v-model.number="draft.bgm.volume" type="number" step="0.01" min="0" max="2" class="max-w-[100px]" />
              <span class="text-xs text-muted-foreground">0~2（1 = 原曲电平，2 = 2 倍）</span>
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">循环策略</Label>
              <Switch v-model="draft.bgm.loop" />
              <span class="text-xs text-muted-foreground">开 = 循环铺满；关 = 只播一遍，其余静音</span>
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">淡入时间</Label>
              <Input v-model.number="draft.bgm.fade_in" type="number" step="0.5" min="0" class="max-w-[100px]" />
              <span class="text-xs text-muted-foreground">秒（混音时钳 章节时长/2）</span>
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">淡出时间</Label>
              <Input v-model.number="draft.bgm.fade_out" type="number" step="0.5" min="0" class="max-w-[100px]" />
              <span class="text-xs text-muted-foreground">秒（混音时钳 章节时长/2）</span>
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">最低匹配分</Label>
              <Input v-model.number="draft.bgm.min_match_score" type="number" min="1" step="1" class="max-w-[100px]" />
              <span class="text-xs text-muted-foreground">低于此分的音乐不进候选</span>
            </div>
          </div>
        </CardContent>
      </Card>
    </template>
  </div>
</template>

<style scoped>
.admin-settings{display:grid;gap:14px}.settings-actions{display:flex;justify-content:flex-end;margin-top:-6px}
</style>
