<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import Sidebar from '@/components/sidebar/Sidebar.vue'

const route = useRoute()
const pageTitle = computed(() => (route.meta.title as string) || '工作台')
</script>

<template>
  <div class="app-shell">
    <Sidebar />
    <main id="main-content" class="app-main">
      <div class="app-topbar">
        <div class="app-breadcrumb">
          <span class="app-breadcrumb__root">Audiobook Studio</span>
          <span class="app-breadcrumb__separator">/</span>
          <span class="app-breadcrumb__current">{{ pageTitle }}</span>
        </div>
        <div class="app-context">本地工作区</div>
      </div>
      <div class="app-content">
        <!-- keep-alive: each module's inputs/toggles/preview survive navigation -->
        <router-view v-slot="{ Component }">
          <keep-alive>
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </div>
    </main>
  </div>
</template>
