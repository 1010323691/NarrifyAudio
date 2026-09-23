<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import {
  LayoutDashboard, Layers, AudioLines, Settings, Type, ScanText, Users,
  Combine, Music4, ShieldCheck, Headphones, FolderOpen,
} from 'lucide-vue-next'
import { useAuthStore } from '@/stores/auth'
import { useAppStore } from '@/stores/app'
import { useSettingsStore } from '@/stores/settings'
import { useWorkspaceStore } from '@/stores/workspace'

const route = useRoute()
const app = useAppStore()
const settings = useSettingsStore()
const auth = useAuthStore()
const workspace = useWorkspaceStore()

const USER_ITEMS = [
  { to: '/dashboard', label: '项目', icon: LayoutDashboard },
  { to: '/resources', label: '我的资源', icon: Layers },
  { to: '/usage', label: '使用量', icon: AudioLines },
  { to: '/settings', label: '设置', icon: Settings },
]

const PROJECT_STAGES = [
  { to: '/text', label: '排版与分册', icon: Type },
  { to: '/script', label: '文本解析', icon: ScanText },
  { to: '/voices', label: '角色配音', icon: Users },
  { to: '/batch', label: '音频合成', icon: Layers },
  { to: '/merge', label: '音频合并', icon: Combine },
  { to: '/audio', label: '音频分集', icon: AudioLines, optional: true },
  { to: '/bgm', label: '背景音乐', icon: Music4 },
]

const ADMIN_ITEMS = [
  { to: '/admin', label: '总览', icon: LayoutDashboard },
  { to: '/admin?tab=performance', label: '性能监控', icon: AudioLines, tab: 'performance' },
  { to: '/admin?tab=users', label: '用户管理', icon: Users, tab: 'users' },
  { to: '/admin?tab=resources', label: '资源管理', icon: Layers, tab: 'resources' },
  { to: '/music', label: '音乐库管理', icon: Music4 },
  { to: '/admin?tab=settings', label: '系统配置', icon: Settings, tab: 'settings' },
  { to: '/admin?tab=tasks', label: '任务 / 队列', icon: Combine, tab: 'tasks' },
  { to: '/admin?tab=logs', label: '日志 / 异常', icon: ScanText, tab: 'logs' },
] as const

const isAdminArea = computed(() => auth.user?.role === 'admin' && ['/admin', '/music'].includes(route.path))
const isProjectContext = computed(() => workspace.hasActiveProject && (
  route.path.startsWith('/projects/') || ['/text', '/script', '/voices', '/batch', '/merge', '/audio', '/bgm'].includes(route.path)
))
const visibleStages = computed(() => PROJECT_STAGES.filter((item) => !item.optional || settings.config?.ui.show_audio_split))

function isActive(to: string) {
  return to === '/dashboard' ? route.path === '/dashboard' : route.path === to
}

function isAdminItemActive(item: (typeof ADMIN_ITEMS)[number]) {
  if (item.to === '/music') return route.path === '/music'
  if (route.path !== '/admin') return false
  return 'tab' in item ? route.query.tab === item.tab : !route.query.tab
}

onMounted(() => { if (!isAdminArea.value && !workspace.loaded) void workspace.refresh() })
</script>

<template>
  <aside class="app-sidebar">
    <RouterLink class="app-brand" :to="isAdminArea ? '/admin' : '/dashboard'" :aria-label="isAdminArea ? '返回管理控制台首页' : '返回工作台首页'">
      <div class="app-brand__mark" aria-hidden="true"><component :is="isAdminArea ? ShieldCheck : Headphones" class="h-5 w-5" /></div>
      <div class="app-brand__copy">
        <div class="app-brand__title">{{ isAdminArea ? '管理控制台' : '有声书工作台' }}</div>
        <div class="app-brand__subtitle">{{ isAdminArea ? 'ADMIN CONSOLE' : 'AUDIOBOOK WORKSPACE' }}</div>
      </div>
    </RouterLink>

    <nav v-if="isAdminArea" class="app-nav" aria-label="系统管理导航">
      <div class="app-nav__group">
        <div class="app-nav__label">系统管理</div>
        <RouterLink v-for="item in ADMIN_ITEMS" :key="item.to" :to="item.to" class="app-nav__item" :class="isAdminItemActive(item) ? 'is-active' : ''">
          <component :is="item.icon" class="app-nav__icon" aria-hidden="true" /><span>{{ item.label }}</span>
        </RouterLink>
      </div>
    </nav>

    <nav v-else class="app-nav" aria-label="Audiobook Production Workspace">
      <div class="app-nav__group">
        <div class="app-nav__label">工作台</div>
        <RouterLink v-for="item in USER_ITEMS" :key="item.to" :to="item.to" class="app-nav__item" :class="isActive(item.to) ? 'is-active' : ''">
          <component :is="item.icon" class="app-nav__icon" aria-hidden="true" /><span>{{ item.label }}</span>
        </RouterLink>
      </div>

      <div v-if="isProjectContext" class="app-nav__group app-nav__project-nav">
        <RouterLink :to="`/projects/${workspace.activeProjectId}`" class="app-nav__label app-nav__project-title">
          <FolderOpen class="h-3.5 w-3.5" /><span>{{ workspace.activeProjectName || '当前项目' }}</span>
        </RouterLink>
        <RouterLink v-for="item in visibleStages" :key="item.to" :to="item.to" class="app-nav__item app-nav__stage-item" :class="isActive(item.to) ? 'is-active' : ''">
          <component :is="item.icon" class="app-nav__icon" aria-hidden="true" /><span>{{ item.label }}</span>
        </RouterLink>
      </div>

    </nav>

    <div v-if="isAdminArea" class="app-status">
      <div class="app-status__row" :class="app.backendUp ? 'is-online' : 'is-offline'"><span class="app-status__dot" aria-hidden="true" /><span>{{ app.backendUp ? '后端已连接' : '后端未连接' }}</span></div>
      <div v-if="app.lastError" class="app-status__error">{{ app.lastError }}</div>
    </div>
  </aside>
</template>
