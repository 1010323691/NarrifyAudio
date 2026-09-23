<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspace'
import { useToast } from '@/components/ui/toast'
import type { AppConfig, TextToggles } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardContent from '@/components/ui/CardContent.vue'
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
  Palette,
  Type,
  Server,
  SlidersHorizontal,
  MessageSquareText,
  AudioWaveform,
  AudioLines,
  Music4,
  UserRound,
  ArrowRight,
} from 'lucide-vue-next'

const settings = useSettingsStore()
const auth = useAuthStore()
const workspace = useWorkspaceStore()
const { push: toast } = useToast()

const draft = ref<AppConfig | null>(null)
const saving = ref(false)
const section = ref<'account' | 'appearance' | 'text' | 'models' | 'audio'>('account')
const workspaceSet = computed(() => workspace.hasActiveProject)

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
  if (!workspace.loaded) await workspace.refresh()
  if (!settings.loaded) await settings.load()
  if (settings.config) draft.value = JSON.parse(JSON.stringify(settings.config))
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
  <div class="user-settings">
    <div class="settings-header">
      <div>
        <p class="eyebrow">YOUR PREFERENCES</p>
        <h1 class="page-title">设置</h1>
        <p class="page-description">账号和界面偏好，以及当前项目的制作参数。</p>
      </div>
      <Button v-if="section !== 'account'" @click="save" :disabled="saving || !draft || !workspaceSet">
        <Save class="h-4 w-4" />{{ saving ? '保存中…' : '保存设置' }}
      </Button>
    </div>

    <nav class="settings-tabs" aria-label="用户设置分类">
      <button v-for="item in ([['account','账号与项目'],['appearance','界面'],['text','文本处理'],['models','解析与 LLM'],['audio','TTS 与音频']] as const)" :key="item[0]" type="button" :class="section === item[0] ? 'is-active' : ''" @click="section = item[0]">{{ item[1] }}</button>
    </nav>

    <Alert v-if="!draft" variant="destructive">
      当前项目设置暂时无法读取。确认项目可用后重试。
    </Alert>

    <template v-else>

      <Card v-if="section === 'account'" class="settings-account">
        <CardHeader><CardTitle class="flex items-center gap-2"><UserRound class="h-5 w-5" />账号</CardTitle></CardHeader>
        <CardContent class="settings-account__body">
          <div><span>显示名称</span><strong>{{ auth.user?.display_name || auth.user?.username || '—' }}</strong></div>
          <div><span>用户名</span><strong>{{ auth.user?.username || '—' }}</strong></div>
          <div><span>邮箱</span><strong>{{ auth.user?.email || '—' }}</strong></div>
          <div><span>账户类型</span><StatusPill :label="auth.user?.role === 'admin' ? '管理员账户' : '个人账户'" :tone="auth.user?.role === 'admin' ? 'neutral' : 'positive'" /></div>
          <div class="settings-current-project"><div><span>当前项目</span><strong>{{ workspace.activeProjectName || '未选择项目' }}</strong></div><RouterLink to="/dashboard">管理项目<ArrowRight class="h-4 w-4" /></RouterLink></div>
          <p class="settings-help">制作参数保存在当前项目中。账户资料和套餐由账户服务管理。</p>
        </CardContent>
      </Card>

      <Alert v-if="section !== 'account' && !workspaceSet" variant="warning">
        先在「我的项目」中打开项目，再编辑项目级偏好。<RouterLink to="/dashboard" class="alert-link">选择项目</RouterLink>
      </Alert>

      <!-- 外观 -->
      <Card v-if="section === 'appearance'">
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

      <!-- 文本排版 -->
      <Card v-if="section === 'text'">
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
      <Card v-if="section === 'models'">
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
      <Card v-if="section === 'models'">
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
      <Card v-if="section === 'models'">
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

      <Card v-if="section === 'audio'">
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
      <Card v-if="section === 'audio'">
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
      <Card v-if="section === 'audio'">
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

    </template>
  </div>
</template>

<style scoped>
.user-settings{display:grid;gap:14px;max-width:1100px;margin:0 auto;padding-bottom:32px}.settings-header{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}.eyebrow{font-size:10px;font-weight:800;letter-spacing:.14em;color:hsl(var(--primary))}.settings-header h1{margin-top:5px}.settings-header .page-description{margin-top:4px}.settings-tabs{display:flex;gap:6px;overflow-x:auto;border-bottom:1px solid hsl(var(--border));padding-bottom:8px}.settings-tabs button{white-space:nowrap;border:1px solid transparent;border-radius:8px;padding:8px 11px;color:hsl(var(--muted-foreground));font-size:12px;font-weight:650}.settings-tabs button.is-active{border-color:hsl(var(--border));background:hsl(var(--card));color:hsl(var(--foreground));box-shadow:0 1px 2px hsl(var(--foreground)/.05)}.settings-account__body{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 20px}.settings-account__body>div{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:46px;border-bottom:1px solid hsl(var(--border));font-size:12px}.settings-account__body>div>span,.settings-account__body>div>div>span{color:hsl(var(--muted-foreground))}.settings-account__body strong{max-width:65%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:650}.settings-current-project{grid-column:1/-1}.settings-current-project>div{display:grid;gap:3px}.settings-current-project a{display:inline-flex;align-items:center;gap:5px;color:hsl(var(--primary));font-size:11px;font-weight:650}.settings-help{grid-column:1/-1;margin-top:10px;color:hsl(var(--muted-foreground));font-size:11px}.alert-link{margin-left:6px;font-weight:700;text-decoration:underline}@media(max-width:600px){.settings-account__body{grid-template-columns:1fr}.settings-current-project{grid-column:auto}.settings-help{grid-column:auto}.settings-tabs button{padding:7px 9px}}
</style>
