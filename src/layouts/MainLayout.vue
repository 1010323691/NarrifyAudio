<script setup lang="ts">
import { computed, onMounted } from 'vue'
import Sidebar from '@/components/sidebar/Sidebar.vue'
import { useWorkspaceStore } from '@/stores/workspace'
import { useAuthStore } from '@/stores/auth'

const workspace = useWorkspaceStore()
const auth = useAuthStore()
const workspaceScope = computed(() => `${auth.user?.id || 'guest'}:${workspace.activeProjectId}`)

onMounted(() => { if (!workspace.loaded) void workspace.refresh() })
</script>

<template>
  <div class="app-shell">
    <Sidebar />
    <main id="main-content" class="app-main">
      <div class="app-content">
        <!-- keep-alive: each module's inputs/toggles/preview survive navigation -->
        <router-view v-slot="{ Component }">
          <keep-alive :key="workspaceScope">
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </div>
    </main>
  </div>
</template>
