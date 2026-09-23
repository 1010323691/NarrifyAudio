<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import {
  LayoutDashboard,
  Type,
  ScanText,
  Users,
  Layers,
  Combine,
  AudioLines,
  Music4,
  Settings,
  Disc3,
  Headphones,
  ShieldCheck,
} from 'lucide-vue-next'
import { useAuthStore } from '@/stores/auth'
import { useAppStore } from '@/stores/app'
import { useSettingsStore } from '@/stores/settings'
import { cn } from '@/lib/utils'

const route = useRoute()
const app = useAppStore()
const settings = useSettingsStore()
const auth = useAuthStore()

const ALL_ITEMS = [
  { to: '/dashboard', label: '开始', icon: LayoutDashboard },
  { to: '/text', label: '排版与分册', icon: Type },
  { to: '/script', label: '文本解析', icon: ScanText },
  { to: '/voices', label: '角色配音', icon: Users },
  { to: '/batch', label: '音频合成', icon: Layers },
  { to: '/merge', label: '音频合并', icon: Combine },
  { to: '/audio', label: '音频分集', icon: AudioLines },
  { to: '/bgm', label: '背景音乐', icon: Music4 },
  { to: '/settings', label: '设置', icon: Settings },
  // 音乐库 = 全局资源（工作空间外、跨工程共享）——与设置同级、放导航栏最下方。
  { to: '/music', label: '音乐库管理', icon: Disc3, admin: true },
  { to: '/admin', label: '管理后台', icon: ShieldCheck, admin: true },
]

const ADMIN_ITEMS = [
  { to: '/admin', label: '总览', icon: LayoutDashboard },
  { to: '/admin?tab=performance', label: '性能监控', icon: AudioLines, tab: 'performance' },
  { to: '/admin?tab=users', label: '用户管理', icon: Users, tab: 'users' },
  { to: '/admin?tab=resources', label: '资源管理', icon: Layers, tab: 'resources' },
  { to: '/music', label: '音乐库管理', icon: Disc3 },
  { to: '/admin?tab=settings', label: '系统配置', icon: Settings, tab: 'settings' },
  { to: '/admin?tab=tasks', label: '任务 / 队列', icon: Combine, tab: 'tasks' },
  { to: '/admin?tab=logs', label: '日志 / 异常', icon: ScanText, tab: 'logs' },
] as const

// 「音频分集」导航项受设置 ui.show_audio_split 控制（默认关 = 隐藏）。
// 背景音乐 / 音乐库恒显示（不受 show_audio_split 过滤）。
const items = computed(() =>
  ALL_ITEMS.filter((it) => (!it.admin || auth.user?.role === 'admin') && (settings.config?.ui.show_audio_split || it.to !== '/audio')),
)
const isAdminArea = computed(() => auth.user?.role === 'admin' && ['/admin', '/music'].includes(route.path))

function isActive(to: string) {
  return route.path === to || (to !== '/dashboard' && route.path.startsWith(to))
}
function isAdminItemActive(item: (typeof ADMIN_ITEMS)[number]) {
  if (item.to === '/music') return route.path === '/music'
  if (route.path !== '/admin') return false
  return 'tab' in item ? route.query.tab === item.tab : !route.query.tab
}
</script>

<template>
  <aside class="app-sidebar">
    <div class="app-brand">
      <div class="app-brand__mark" aria-hidden="true">
        <Headphones class="h-5 w-5" />
      </div>
      <div class="app-brand__copy">
        <div class="app-brand__title">有声书工作台</div>
        <div class="app-brand__subtitle">Audiobook Studio</div>
      </div>
    </div>

    <nav v-if="isAdminArea" class="app-nav" aria-label="系统管理导航">
      <div class="app-nav__group">
        <div class="app-nav__label">系统管理</div>
        <RouterLink v-for="it in ADMIN_ITEMS" :key="it.to" :to="it.to" class="app-nav__item" :class="isAdminItemActive(it) ? 'is-active' : ''">
          <component :is="it.icon" class="app-nav__icon" aria-hidden="true" />
          <span>{{ it.label }}</span>
        </RouterLink>
      </div>
    </nav>

    <nav v-else class="app-nav" aria-label="主导航">
      <div class="app-nav__group">
        <div class="app-nav__label">制作流程</div>
        <RouterLink
          v-for="it in items.filter((item) => !['/settings', '/music', '/admin'].includes(item.to))"
          :key="it.to"
          :to="it.to"
          class="app-nav__item"
          :class="isActive(it.to) ? 'is-active' : ''"
        >
          <component :is="it.icon" class="app-nav__icon" aria-hidden="true" />
          <span>{{ it.label }}</span>
        </RouterLink>
      </div>

      <div class="app-nav__group app-nav__group--secondary">
        <div class="app-nav__label">资源与设置</div>
        <RouterLink
          v-for="it in items.filter((item) => item.to === '/settings')"
          :key="it.to"
          :to="it.to"
          class="app-nav__item"
          :class="isActive(it.to) ? 'is-active' : ''"
        >
          <component :is="it.icon" class="app-nav__icon" aria-hidden="true" />
          <span>{{ it.label }}</span>
        </RouterLink>
      </div>

      <div v-if="auth.user?.role === 'admin'" class="app-nav__group app-nav__group--secondary">
        <div class="app-nav__label">系统管理</div>
        <RouterLink v-for="it in items.filter((item) => ['/admin', '/music'].includes(item.to))" :key="it.to" :to="it.to" class="app-nav__item" :class="isActive(it.to) ? 'is-active' : ''">
          <component :is="it.icon" class="app-nav__icon" aria-hidden="true" /><span>{{ it.label }}</span>
        </RouterLink>
      </div>
    </nav>

    <div class="app-status">
      <div class="app-status__row" :class="app.backendUp ? 'is-online' : 'is-offline'">
        <span class="app-status__dot" aria-hidden="true" />
        <span>{{ app.backendUp ? '后端已连接' : '后端未连接' }}</span>
      </div>
      <div v-if="app.lastError" class="app-status__error">
        {{ app.lastError }}
      </div>
    </div>
  </aside>
</template>
