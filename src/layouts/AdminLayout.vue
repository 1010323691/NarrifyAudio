<script setup lang="ts">
import '@/styles/admin.css'
import { useAuthStore } from '@/stores/auth'
import Sidebar from '@/components/sidebar/Sidebar.vue'
const auth = useAuthStore()
</script>

<template>
  <div class="app-shell admin-shell">
    <Sidebar />
    <main id="main-content" class="app-main">
      <div class="app-content app-content--admin">
        <router-view v-slot="{ Component }">
          <keep-alive :key="auth.user?.id">
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </div>
    </main>
  </div>
</template>
