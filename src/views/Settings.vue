<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useAppStore } from '@/stores/app'
import { useToast } from '@/components/ui/toast'
import { useWorkspaceGate } from '@/composables/useWorkspaceGate'
import { health } from '@/api/client'
import type { AppConfig, TextToggles } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardContent from '@/components/ui/CardContent.vue'
import CardFooter from '@/components/ui/CardFooter.vue'
import Input from '@/components/ui/Input.vue'
import Label from '@/components/ui/Label.vue'
import Textarea from '@/components/ui/Textarea.vue'
import Switch from '@/components/ui/Switch.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import Alert from '@/components/ui/Alert.vue'
import {
  Save,
  Monitor,
  Sun,
  Moon,
  RefreshCw,
  Palette,
  FolderCog,
  Type,
  Server,
  SlidersHorizontal,
  MessageSquareText,
  AudioWaveform,
  AudioLines,
  Music4,
} from 'lucide-vue-next'

const settings = useSettingsStore()
const app = useAppStore()
const { push: toast } = useToast()
const { workspaceSet } = useWorkspaceGate()

const draft = ref<AppConfig | null>(null)
const saving = ref(false)
const healthInfo = ref<{ ok: boolean; service: string; port: number } | null>(null)

const TOGGLES: { key: keyof TextToggles; label: string }[] = [
  { key: 'sentence_break', label: '断句换段' },
  { key: 'dialogue_separate', label: '对话独立成段' },
  { key: 'detect_chapters', label: '识别章节标题' },
  { key: 'keep_single_space', label: '保留单个空格' },
  { key: 'punct_ellipsis', label: '省略号统一' },
  { key: 'punct_repeated', label: '合并重复标点' },
  { key: 'punct_quotes', label: '引号成对' },
  { key: 'punct_lone_ascii', label: '半角标点转全角' },
  { key: 'punct_dash', label: '破折号统一' },
  { key: 'live', label: '实时预览' },
]

const THEMES = [
  { key: 'system', label: '跟随系统', icon: Monitor },
  { key: 'light', label: '浅色', icon: Sun },
  { key: 'dark', label: '深色', icon: Moon },
]

onMounted(async () => {
  app.ping()
  if (!settings.loaded) await settings.load()
  if (settings.config) draft.value = JSON.parse(JSON.stringify(settings.config))
  try {
    healthInfo.value = await health()
  } catch {
    healthInfo.value = null
  }
})

function setTheme(theme: string) {
  if (!draft.value) return
  draft.value.ui.theme = theme
  settings.applyTheme(theme) // apply immediately; persisted on 保存
}

async function save() {
  if (!draft.value) return
  const cap = Number(draft.value.tts.batch_concurrency)
  draft.value.tts.batch_concurrency = Number.isFinite(cap) ? Math.max(1, Math.min(128, Math.trunc(cap))) : 80
  saving.value = true
  const ok = await settings.save(draft.value)
  saving.value = false
  if (ok) toast({ title: '设置已保存', variant: 'success', description: '已写入工作空间的 config/app.json，重启后自动恢复。' })
  else toast({ title: '保存失败', variant: 'destructive' })
}
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center justify-between">
      <div>
        <h1 class="text-2xl font-bold tracking-tight">设置</h1>
        <p class="mt-1 text-muted-foreground">配置应用和生成参数，设置会保存到当前工作空间。</p>
      </div>
      <Button @click="save" :disabled="saving || !draft || !workspaceSet">
        <Save class="h-4 w-4" />{{ saving ? '保存中…' : '保存设置' }}
      </Button>
    </div>

    <Alert v-if="!draft" variant="destructive">
      无法加载配置——请确认后端已启动（127.0.0.1:8642）。
    </Alert>

    <template v-else>
      <!-- 未设工作空间时提示（配置随工程，未开工不可保存） -->
      <Alert v-if="!workspaceSet" variant="warning">
        请先在「开始」页选择工作空间。
      </Alert>

      <!-- 后端状态 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><RefreshCw class="h-5 w-5" />后端状态</CardTitle>
        </CardHeader>
        <CardContent class="flex flex-wrap items-center gap-3">
          <StatusPill
            :label="app.backendUp ? '已连接' : '未连接'"
            :tone="app.backendUp ? 'positive' : 'negative'"
            :aria-label="app.backendUp ? '后端已连接' : '后端未连接'"
          />
          <span v-if="healthInfo" class="text-sm text-muted-foreground">
            {{ healthInfo.service }} · 端口 {{ healthInfo.port }}
          </span>
          <Button variant="outline" size="sm" class="ml-auto" @click="app.ping()">
            <RefreshCw class="h-4 w-4" />重新检测
          </Button>
        </CardContent>
      </Card>

      <!-- 外观 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Palette class="h-5 w-5" />外观主题</CardTitle>
        </CardHeader>
        <CardContent class="space-y-4">
          <div class="flex gap-2">
            <Button
              v-for="t in THEMES"
              :key="t.key"
              :variant="draft.ui.theme === t.key ? 'default' : 'outline'"
              @click="setTheme(t.key)"
            >
              <component :is="t.icon" class="h-4 w-4" />{{ t.label }}
            </Button>
          </div>
          <div class="space-y-1">
            <div class="flex items-center justify-between">
              <Label class="font-normal">解析日志显示</Label>
              <Switch v-model="draft.ui.show_parse_logs" />
            </div>
            <p class="text-xs text-muted-foreground">
              开启后，文本解析页显示详细日志。
            </p>
          </div>
          <div class="space-y-1">
            <div class="flex items-center justify-between">
              <Label class="font-normal">音频分集导航项</Label>
              <Switch v-model="draft.ui.show_audio_split" />
            </div>
            <p class="text-xs text-muted-foreground">
              开启后在侧边栏显示「音频分集」。
            </p>
          </div>
        </CardContent>
      </Card>

      <!-- 工作空间（在「开始」页设置，此处只读） -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><FolderCog class="h-5 w-5" />工作空间</CardTitle>
        </CardHeader>
        <CardContent class="space-y-2">
          <Label>目录</Label>
          <Input
            :model-value="draft.paths.working_dir"
            readonly
            :placeholder="workspaceSet ? '' : '未设置——请在「开始」页选择文件夹'"
          />
          <p class="text-xs text-muted-foreground">
            工作空间在「开始」页选择。
          </p>
        </CardContent>
      </Card>

      <!-- 文本排版 -->
      <Card>
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

      <!-- LLM 配置 -->
      <Card>
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
              <Input v-model="draft.llm.model_name" placeholder="如 qwen3:14b（必填）" />
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- 生成参数 -->
      <Card>
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
            <Label>并发数（同时解析的文件数）</Label>
            <div class="flex flex-wrap items-center gap-3">
              <Input v-model.number="draft.generation.max_concurrency" type="number" min="1" step="1" class="max-w-[8rem]" />
              <span class="text-xs text-muted-foreground">
                超出并发数的文件会排队。
              </span>
            </div>
          </div>
          <div class="space-y-1.5">
            <Label>归属抽样率（0 = 关闭）</Label>
            <div class="flex flex-wrap items-center gap-3">
              <Input v-model.number="draft.generation.spot_check_rate" type="number" step="0.01" min="0" max="0.5" class="max-w-[8rem]" />
              <span class="text-xs text-muted-foreground">
                设置解析结果的抽样复核比例；0 表示关闭。
              </span>
            </div>
          </div>
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <div class="flex items-center justify-between">
              <Label class="font-normal">角色匹配检查</Label>
              <Switch v-model="draft.generation.check_boundary_speakers" />
            </div>
            <div class="flex items-center justify-between">
              <Label class="font-normal">断句失败校验</Label>
              <Switch v-model="draft.generation.revalidate_splits" />
            </div>
            <div class="flex items-center justify-between">
              <Label class="font-normal">纯归属标签删除</Label>
              <Switch v-model="draft.generation.delete_saying_tags" />
            </div>
            <div class="flex items-center justify-between">
              <div class="flex flex-col">
                <Label class="font-normal">超长段落检查</Label>
                <span class="text-xs text-muted-foreground">
                  设置单段最大字数。
                </span>
              </div>
              <div class="flex items-center gap-2">
                <Input
                  v-model.number="draft.generation.max_paragraph_chars"
                  type="number" min="10" step="10" class="w-20"
                />
                <Switch v-model="draft.generation.check_long_paragraphs" />
              </div>
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
      <Card>
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

      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><AudioWaveform class="h-5 w-5" />TTS 批处理</CardTitle>
          <CardDescription>按文本长度排序后组批，仅使用批内上限。</CardDescription>
        </CardHeader>
        <CardContent>
          <div class="space-y-1.5">
            <Label for="tts-batch-limit">批内上限</Label>
            <div class="flex flex-wrap items-center gap-3">
              <Input id="tts-batch-limit" v-model.number="draft.tts.batch_concurrency" type="number" min="1" max="128" step="1" class="max-w-[120px]" :disabled="draft.tts.batch_auto" />
              <label class="flex items-center gap-2 text-sm text-muted-foreground" title="按同批最长文本字数向上匹配已测安全档位">
                <input v-model="draft.tts.batch_auto" type="checkbox" class="h-4 w-4 rounded border-input accent-primary" />
                自动
              </label>
            </div>
            <p class="text-xs text-muted-foreground">自动模式按同批最长文本字数向上匹配已测安全档位。</p>
          </div>
        </CardContent>
      </Card>

      <!-- 音频分集 -->
      <Card>
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
      <Card>
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
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">分析采样字数</Label>
              <Input v-model.number="draft.bgm.analysis_chars" type="number" min="500" step="500" class="max-w-[100px]" />
              <span class="text-xs text-muted-foreground">章节 LLM 气氛分析的头/中/尾采样字数</span>
            </div>
          </div>
        </CardContent>
      </Card>

      <div class="flex justify-end">
        <Button @click="save" :disabled="saving || !workspaceSet">
          <Save class="h-4 w-4" />{{ saving ? '保存中…' : '保存设置' }}
        </Button>
      </div>
    </template>
  </div>
</template>
