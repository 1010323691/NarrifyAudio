<script setup lang="ts">
import { computed, onMounted } from 'vue'
import Sidebar from '@/components/sidebar/Sidebar.vue'
import { useProjectStore } from '@/stores/project'
import { useAuthStore } from '@/stores/auth'

const project = useProjectStore()
const auth = useAuthStore()
const projectScope = computed(() => `${auth.user?.id || 'guest'}:${project.activeProjectId}`)

onMounted(() => { if (!project.loaded) void project.refresh() })
</script>

<template>
  <div class="app-shell">
    <Sidebar />
    <main id="main-content" class="app-main">
      <div class="app-content">
        <!-- keep-alive: each module's inputs/toggles/preview survive navigation -->
        <router-view v-slot="{ Component }">
          <keep-alive :key="projectScope">
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </div>
    </main>
  </div>
</template>
