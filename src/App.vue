<script setup lang="ts">
import { onMounted } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import Toaster from '@/components/ui/Toaster.vue'
import { useAuthStore } from '@/stores/auth'

const settings = useSettingsStore()
const auth = useAuthStore()

// Apply the persisted theme as early as possible (falls back quietly if the
// backend isn't up yet).
onMounted(() => {
  if (!settings.loaded) settings.load()
  auth.load()
})
</script>

<template>
  <router-view />
  <Toaster />
</template>
