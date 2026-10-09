<script setup lang="ts">
import { computed, type Component } from 'vue'
import { useRoute } from 'vue-router'
import AdminOverview from './admin/AdminOverview.vue'
import AdminMonitor from './admin/AdminMonitor.vue'
import AdminTasks from './admin/AdminTasks.vue'
import AdminUsers from './admin/AdminUsers.vue'
import AdminResources from './admin/AdminResources.vue'
import AdminLogs from './admin/AdminLogs.vue'
import AdminConfig from './admin/AdminConfig.vue'

// `?tab=` keeps the console's existing links (sidebar, bookmarks, tests).
const views: Record<string, Component> = {
  overview: AdminOverview, performance: AdminMonitor, tasks: AdminTasks, users: AdminUsers,
  resources: AdminResources, logs: AdminLogs, settings: AdminConfig,
}
const route = useRoute()
const tab = computed(() => typeof route.query.tab === 'string' && route.query.tab in views ? route.query.tab : 'overview')
</script>

<template>
  <!-- Each view's root is the bounded `.viewport-page` (direct child of .app-content). -->
  <KeepAlive>
    <component :is="views[tab]" :key="tab" />
  </KeepAlive>
</template>
