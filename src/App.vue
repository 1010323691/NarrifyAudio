<script setup lang="ts">
import { onMounted } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import Toaster from '@/components/ui/Toaster.vue'

const settings = useSettingsStore()

// Apply the persisted theme as early as possible (falls back quietly if the
// backend isn't up yet).
onMounted(() => {
  if (!settings.loaded) settings.load()
})
</script>

<template>
  <router-view />
  <Toaster />
</template>
