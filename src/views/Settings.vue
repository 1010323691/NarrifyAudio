<script setup lang="ts">
// 用户偏好页（S9：管理端已拆至 components/settings/AdminSettings.vue）。
// 只走项目配置通道（settings.load/save）——保存的仅是 ui 偏好。
import { computed, onMounted, ref } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'
import { useToast } from '@/components/ui/toast'
import type { AppConfig } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardContent from '@/components/ui/CardContent.vue'
import Label from '@/components/ui/Label.vue'
import Switch from '@/components/ui/Switch.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import Alert from '@/components/ui/Alert.vue'
import {
  Save,
  Monitor,
  Sun,
  Moon,
  Palette,
  UserRound,
  ArrowRight,
} from 'lucide-vue-next'

const settings = useSettingsStore()
const auth = useAuthStore()
const project = useProjectStore()
const { push: toast } = useToast()

const draft = ref<AppConfig | null>(null)
const saving = ref(false)
const section = ref<'account' | 'appearance'>('account')
const projectSet = computed(() => project.hasActiveProject)
const settingsTabs = [['account','账号与项目'],['appearance','界面']] as const

const THEMES = [
  { key: 'system', label: '跟随系统', icon: Monitor },
  { key: 'light', label: '浅色', icon: Sun },
  { key: 'dark', label: '深色', icon: Moon },
]

onMounted(async () => {
  if (!project.loaded) await project.refresh()
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
  saving.value = true
  const ok = await settings.save({ ui: draft.value.ui })
  saving.value = false
  if (ok) toast({ title: '设置已保存', variant: 'success', description: '界面偏好已保存。' })
  else toast({ title: '保存失败', variant: 'destructive' })
}
</script>

<template>
  <div class="user-settings">
    <div class="settings-header">
      <div>
        <p class="eyebrow">YOUR PREFERENCES</p>
        <h1 class="page-title">设置</h1>
        <p class="page-description">管理账号信息和个人界面偏好。</p>
      </div>
      <Button v-if="section !== 'account'" @click="save" :disabled="saving || !draft || !projectSet">
        <Save class="h-4 w-4" />{{ saving ? '保存中…' : '保存设置' }}
      </Button>
    </div>

    <nav class="settings-tabs" aria-label="用户偏好分类">
      <button v-for="item in settingsTabs" :key="item[0]" type="button" :class="section === item[0] ? 'is-active' : ''" @click="section = item[0]">{{ item[1] }}</button>
    </nav>

    <Alert v-if="!draft" variant="destructive">当前项目设置暂时无法读取。确认项目可用后重试。</Alert>

    <template v-else>
      <Card v-if="section === 'account'" class="settings-account">
        <CardHeader><CardTitle class="flex items-center gap-2"><UserRound class="h-5 w-5" />账号</CardTitle></CardHeader>
        <CardContent class="settings-account__body">
          <div><span>显示名称</span><strong>{{ auth.user?.display_name || auth.user?.username || '—' }}</strong></div>
          <div><span>用户名</span><strong>{{ auth.user?.username || '—' }}</strong></div>
          <div><span>邮箱</span><strong>{{ auth.user?.email || '—' }}</strong></div>
          <div><span>账户类型</span><StatusPill :label="auth.user?.role === 'admin' ? '管理员账户' : '个人账户'" :tone="auth.user?.role === 'admin' ? 'neutral' : 'positive'" /></div>
          <div class="settings-current-project"><div><span>当前项目</span><strong>{{ project.activeProjectName || '未选择项目' }}</strong></div><RouterLink to="/dashboard">管理项目<ArrowRight class="h-4 w-4" /></RouterLink></div>
          <p class="settings-help">制作参数保存在当前项目中。账户资料和套餐由账户服务管理。</p>
        </CardContent>
      </Card>

      <Alert v-if="section !== 'account' && !projectSet" variant="warning">
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
              <Label class="font-normal">音频分集导航项</Label>
              <Switch v-model="draft.ui.show_audio_split" />
            </div>
            <p class="text-xs text-muted-foreground">
              开启后在侧边栏显示「音频分集」。
            </p>
          </div>
        </CardContent>
      </Card>
    </template>
  </div>
</template>

<style scoped>
.user-settings{display:grid;gap:14px;max-width:1100px;margin:0 auto;padding-bottom:32px}.settings-header{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}.eyebrow{font-size:10px;font-weight:800;letter-spacing:.14em;color:hsl(var(--primary))}.settings-header h1{margin-top:5px}.settings-header .page-description{margin-top:4px}.settings-tabs{display:flex;gap:6px;overflow-x:auto;border-bottom:1px solid hsl(var(--border));padding-bottom:8px}.settings-tabs button{white-space:nowrap;border:1px solid transparent;border-radius:8px;padding:8px 11px;color:hsl(var(--muted-foreground));font-size:12px;font-weight:650}.settings-tabs button.is-active{border-color:var(--glass-border);background:var(--glass-tint-strong);box-shadow:var(--glass-highlight);color:hsl(var(--foreground))}.settings-account__body{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 20px}.settings-account__body>div{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:46px;border-bottom:1px solid hsl(var(--border));font-size:12px}.settings-account__body>div>span,.settings-account__body>div>div>span{color:hsl(var(--muted-foreground))}.settings-account__body strong{max-width:65%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:650}.settings-current-project{grid-column:1/-1}.settings-current-project>div{display:grid;gap:3px}.settings-current-project a{display:inline-flex;align-items:center;gap:5px;color:hsl(var(--primary));font-size:11px;font-weight:650}.settings-help{grid-column:1/-1;margin-top:10px;color:hsl(var(--muted-foreground));font-size:11px}.alert-link{margin-left:6px;font-weight:700;text-decoration:underline}@media(max-width:600px){.settings-account__body{grid-template-columns:1fr}.settings-current-project{grid-column:auto}.settings-help{grid-column:auto}.settings-tabs button{padding:7px 9px}}
</style>
