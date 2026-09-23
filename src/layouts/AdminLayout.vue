<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { LogOut } from 'lucide-vue-next'
import Sidebar from '@/components/sidebar/Sidebar.vue'
import Button from '@/components/ui/Button.vue'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const auth = useAuthStore()
const pageTitle = computed(() => (route.meta.title as string) || '管理控制台')

async function signOut() {
  try {
    await auth.signOut()
  } finally {
    window.location.hash = '#/admin/login'
  }
}
</script>

<template>
  <div class="app-shell">
    <Sidebar />
    <main id="main-content" class="app-main">
      <div class="app-topbar">
        <div class="app-breadcrumb">
          <span class="app-breadcrumb__root">Admin Console</span>
          <span class="app-breadcrumb__separator">/</span>
          <span class="app-breadcrumb__current">{{ pageTitle }}</span>
        </div>
        <div class="flex items-center gap-3">
          <div class="app-context" :title="`管理员 ID：${auth.user?.id || ''}`">管理员 ID · {{ auth.user?.id || '—' }}</div>
          <Button variant="ghost" size="sm" aria-label="退出管理员登录" @click="signOut"><LogOut class="h-4 w-4" />退出</Button>
        </div>
      </div>
      <div class="app-content">
        <router-view v-slot="{ Component }">
          <keep-alive>
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </div>
    </main>
  </div>
</template>
