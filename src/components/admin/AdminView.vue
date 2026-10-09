<script setup lang="ts">
import AdminPageHeader from './AdminPageHeader.vue'
import AdminLoadingState from './AdminLoadingState.vue'
import AdminRefreshControls from './AdminRefreshControls.vue'
import type { AdminLoaderHandle } from '@/composables/useAdminLoader'

defineProps<{ title: string; description: string; loader: AdminLoaderHandle }>()
</script>

<template>
  <div class="admin-console admin-view viewport-page">
    <AdminPageHeader :title="title" :description="description">
      <slot name="actions" />
      <AdminRefreshControls :loader="loader" />
    </AdminPageHeader>
    <div class="page-region" role="region" aria-label="页面工作区" tabindex="0">
      <p v-if="loader.error.value" class="admin-error" role="alert"><strong>无法完成请求</strong> · {{ loader.error.value }} <button type="button" @click="loader.load()">重新尝试</button></p>
      <AdminLoadingState v-if="loader.loading.value && !loader.loaded.value" />
      <div v-show="loader.loaded.value" class="admin-section" :aria-busy="loader.loading.value">
        <slot />
      </div>
    </div>
  </div>
</template>
