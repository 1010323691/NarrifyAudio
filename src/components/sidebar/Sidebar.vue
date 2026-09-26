<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  LayoutDashboard, Layers, AudioLines, Settings, Type, ScanText, Users,
  Combine, Music4, ShieldCheck, FolderOpen, LogOut, Monitor, Sun, Moon, ListTodo, Trash2,
} from 'lucide-vue-next'
import { useAuthStore } from '@/stores/auth'
import { useAppStore } from '@/stores/app'
import { useSettingsStore } from '@/stores/settings'
import { useProjectStore } from '@/stores/project'

const route = useRoute()
const app = useAppStore()
const settings = useSettingsStore()
const auth = useAuthStore()
const project = useProjectStore()
const accountMenuOpen = ref(false)
const accountMenuRoot = ref<HTMLDivElement | null>(null)
const accountTrigger = ref<HTMLButtonElement | null>(null)
const displayName = computed(() => auth.user?.display_name || auth.user?.username || '账户')
const username = computed(() => auth.user?.username || displayName.value)
const avatarInitial = computed(() => displayName.value.trim().charAt(0).toUpperCase() || 'U')
const themeOptions = [
  { key: 'system', label: '跟随系统', icon: Monitor },
  { key: 'light', label: '浅色', icon: Sun },
  { key: 'dark', label: '深色', icon: Moon },
] as const
const activeTheme = computed(() => settings.config?.ui.theme || 'system')

const USER_ITEMS = [
  { to: '/dashboard', label: '项目', icon: LayoutDashboard },
  { to: '/tasks', label: '任务中心', icon: ListTodo },
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
  { to: '/admin/music', label: '音乐库管理', icon: Music4 },
  { to: '/admin?tab=settings', label: '系统配置', icon: Settings, tab: 'settings' },
  { to: '/admin?tab=tasks', label: '任务 / 队列', icon: Combine, tab: 'tasks' },
  { to: '/admin?tab=logs', label: '日志 / 异常', icon: ScanText, tab: 'logs' },
] as const

const isAdminArea = computed(() => auth.user?.role === 'admin' && (route.path === '/admin' || route.path.startsWith('/admin/')))
const visibleStages = computed(() => PROJECT_STAGES.filter((item) => !item.optional || settings.config?.ui.show_audio_split))
watch(() => route.fullPath, () => { accountMenuOpen.value = false })

function isActive(to: string) {
  return to === '/dashboard' ? route.path === '/dashboard' : route.path === to
}

function isAdminItemActive(item: (typeof ADMIN_ITEMS)[number]) {
  if (item.to === '/admin/music') return route.path === '/admin/music'
  if (route.path !== '/admin') return false
  return 'tab' in item ? route.query.tab === item.tab : !route.query.tab
}

function closeAccountMenu() {
  accountMenuOpen.value = false
  accountTrigger.value?.focus()
}

function handleDocumentPointerDown(event: PointerEvent) {
  const target = event.target
  if (target instanceof Node && !accountMenuRoot.value?.contains(target)) {
    accountMenuOpen.value = false
  }
}

async function setTheme(theme: (typeof themeOptions)[number]['key']) {
  const previousTheme = activeTheme.value
  if (theme === previousTheme) return
  settings.applyTheme(theme)
  const saved = await settings.save({ ui: { theme } })
  if (!saved) settings.applyTheme(previousTheme)
}

async function signOut() {
  const adminSignOut = isAdminArea.value
  try {
    await auth.signOut()
  } finally {
    window.location.hash = adminSignOut ? '#/admin/login' : '#/login'
  }
}

onMounted(() => {
  document.addEventListener('pointerdown', handleDocumentPointerDown)
  if (!isAdminArea.value && !project.loaded) void project.refresh()
})
onBeforeUnmount(() => document.removeEventListener('pointerdown', handleDocumentPointerDown))
</script>

<template>
  <aside class="app-sidebar">
    <RouterLink class="app-brand" :to="isAdminArea ? '/admin' : '/dashboard'" :aria-label="isAdminArea ? '返回管理控制台首页' : '返回工作台首页'">
      <div class="app-brand__mark" :class="{ 'app-brand__mark--image': !isAdminArea }" aria-hidden="true">
        <img v-if="!isAdminArea" class="app-brand__image" src="/narrify-audio-icon.png" alt="" />
        <ShieldCheck v-else class="h-5 w-5" />
      </div>
      <div class="app-brand__copy">
        <div class="app-brand__title">{{ isAdminArea ? '管理控制台' : '有声书工作台' }}</div>
        <div class="app-brand__subtitle">{{ isAdminArea ? 'ADMIN CONSOLE' : 'NARRIFY AUDIO WORKSPACE' }}</div>
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

    <nav v-else class="app-nav" aria-label="Narrify Audio Production Workspace">
      <div class="app-nav__group">
        <div class="app-nav__label">工作台</div>
        <RouterLink v-for="item in USER_ITEMS" :key="item.to" :to="item.to" class="app-nav__item" :class="isActive(item.to) ? 'is-active' : ''">
          <component :is="item.icon" class="app-nav__icon" aria-hidden="true" /><span>{{ item.label }}</span>
        </RouterLink>
      </div>

      <div v-if="project.hasActiveProject" class="app-nav__group app-nav__project-nav">
        <RouterLink :to="`/projects/${project.activeProjectId}`" class="app-nav__label app-nav__project-title">
          <FolderOpen class="h-3.5 w-3.5" /><span class="app-nav__project-name">{{ project.activeProjectName || '当前项目' }}</span>
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
    <div
      ref="accountMenuRoot"
      class="app-account"
      @keydown.esc.prevent="closeAccountMenu"
    >
      <button
        ref="accountTrigger"
        type="button"
        class="app-account__trigger"
        :aria-label="`账户菜单：${username}`"
        aria-haspopup="true"
        :aria-expanded="accountMenuOpen"
        :title="username"
        @click="accountMenuOpen = !accountMenuOpen"
      >
        <span class="app-account__avatar" aria-hidden="true">{{ avatarInitial }}</span>
        <span class="app-account__username">{{ username }}</span>
      </button>

      <Transition name="account-popover">
        <div v-if="accountMenuOpen" class="app-account__menu" role="group" aria-label="账户菜单">
        <div class="app-account__profile" role="group" aria-label="账户信息">
          <span class="app-account__avatar app-account__avatar--large" aria-hidden="true">{{ avatarInitial }}</span>
          <div class="app-account__profile-copy"><strong>{{ displayName }}</strong><small>{{ auth.user?.email }}</small></div>
        </div>
        <div class="app-account__theme" role="group" aria-label="主题切换">
          <button
            v-for="theme in themeOptions"
            :key="theme.key"
            type="button"
            class="app-account__theme-button"
            :disabled="settings.saving"
            :class="{ 'is-active': activeTheme === theme.key }"
            :aria-label="theme.label"
            :aria-pressed="activeTheme === theme.key"
            :title="theme.label"
            @click="setTheme(theme.key)"
          ><component :is="theme.icon" class="h-4 w-4" aria-hidden="true" /></button>
        </div>
        <div class="app-account__theme-divider" aria-hidden="true" />
        <div class="app-account__menu-list">
          <template v-if="isAdminArea">
            <RouterLink to="/admin?tab=settings" class="app-account__menu-item" @click="accountMenuOpen = false"><Settings class="h-4 w-4" />系统配置</RouterLink>
          </template>
          <template v-else>
            <RouterLink to="/usage" class="app-account__menu-item" @click="accountMenuOpen = false"><AudioLines class="h-4 w-4" />使用量</RouterLink>
            <RouterLink to="/resources" class="app-account__menu-item" @click="accountMenuOpen = false"><Layers class="h-4 w-4" />我的资源</RouterLink>
            <RouterLink to="/trash" class="app-account__menu-item" @click="accountMenuOpen = false"><Trash2 class="h-4 w-4" aria-hidden="true" />回收站</RouterLink>
            <RouterLink to="/settings" class="app-account__menu-item" @click="accountMenuOpen = false"><Settings class="h-4 w-4" />设置</RouterLink>
          </template>
          <button type="button" class="app-account__menu-item app-account__menu-item--logout" @click="signOut"><LogOut class="h-4 w-4" />退出登录</button>
        </div>
        </div>
      </Transition>
    </div>
  </aside>
</template>
