<script setup lang="ts">
import { RefreshCw } from 'lucide-vue-next'
import Button from '@/components/ui/Button.vue'
import { refreshInterval, refreshIntervalOptions, type AdminLoaderHandle } from '@/composables/useAdminLoader'

defineProps<{ loader: AdminLoaderHandle }>()
const intervalLabel = (value: number) => value ? `每 ${value / 1000} 秒` : '关闭'
</script>

<template>
  <span v-if="loader.lastUpdated.value" class="admin-updated"><span class="admin-live-dot" :class="{ 'is-off': !loader.poll || !refreshInterval }" aria-hidden="true" />更新于 {{ loader.lastUpdated.value }}</span>
  <label v-if="loader.poll" class="admin-select-inline">
    <span>自动刷新</span>
    <select v-model.number="refreshInterval" aria-label="自动刷新间隔">
      <option v-for="value in refreshIntervalOptions" :key="value" :value="value">{{ intervalLabel(value) }}</option>
    </select>
  </label>
  <Button variant="outline" size="sm" :disabled="loader.loading.value || loader.actionBusy.value" @click="loader.load()">
    <RefreshCw class="h-4 w-4" :class="{ 'animate-spin': loader.loading.value }" />{{ loader.loading.value ? '刷新中' : '刷新' }}
  </Button>
</template>
