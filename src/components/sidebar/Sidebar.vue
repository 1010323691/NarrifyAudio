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
} from 'lucide-vue-next'
import { useAppStore } from '@/stores/app'
import { useSettingsStore } from '@/stores/settings'
import { cn } from '@/lib/utils'

const route = useRoute()
const app = useAppStore()
const settings = useSettingsStore()

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
  { to: '/music', label: '音乐库', icon: Disc3 },
]

// 「音频分集」导航项受设置 ui.show_audio_split 控制（默认关 = 隐藏）。
// 背景音乐 / 音乐库恒显示（不受 show_audio_split 过滤）。
const items = computed(() =>
  settings.config?.ui.show_audio_split
    ? ALL_ITEMS
    : ALL_ITEMS.filter((it) => it.to !== '/audio'),
)

function isActive(to: string) {
  return route.path === to || (to !== '/dashboard' && route.path.startsWith(to))
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

    <nav class="app-nav" aria-label="主导航">
      <div class="app-nav__group">
        <div class="app-nav__label">制作流程</div>
        <RouterLink
          v-for="it in items.filter((item) => !['/settings', '/music'].includes(item.to))"
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
          v-for="it in items.filter((item) => ['/settings', '/music'].includes(item.to))"
          :key="it.to"
          :to="it.to"
          class="app-nav__item"
          :class="isActive(it.to) ? 'is-active' : ''"
        >
          <component :is="it.icon" class="app-nav__icon" aria-hidden="true" />
          <span>{{ it.label }}</span>
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
