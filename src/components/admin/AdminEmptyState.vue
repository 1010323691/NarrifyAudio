<script setup lang="ts">
import { Inbox } from 'lucide-vue-next'
defineProps<{ title: string; description?: string }>()
</script>

<template>
  <div class="admin-empty-state" role="status">
    <Inbox class="h-5 w-5" aria-hidden="true" />
    <strong>{{ title }}</strong><p v-if="description">{{ description }}</p><slot />
  </div>
</template>
