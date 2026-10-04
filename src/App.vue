<script setup lang="ts">
import { onMounted, onBeforeUnmount, watch } from 'vue'
import { useClientDisplayStore } from '@/stores/clientDisplay'
import { useSettingsStore } from '@/stores/settings'
import Toaster from '@/components/ui/Toaster.vue'
import DialogHost from '@/components/ui/DialogHost.vue'
import { useAuthStore } from '@/stores/auth'

const settings = useSettingsStore()
const auth = useAuthStore()
const clientDisplay = useClientDisplayStore()
watch(() => auth.user?.id, () => {
  clientDisplay.reset()
  if (auth.user) void clientDisplay.load()
})
function refreshClientDisplay() {
  if (auth.user && !clientDisplay.saving && document.visibilityState === 'visible') void clientDisplay.load()
}
const displayTimer = setInterval(refreshClientDisplay, 5000)
document.addEventListener('visibilitychange', refreshClientDisplay)
onBeforeUnmount(() => {
  clearInterval(displayTimer)
  document.removeEventListener('visibilitychange', refreshClientDisplay)
})

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
  <DialogHost />
</template>
