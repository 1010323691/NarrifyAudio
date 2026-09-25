<script setup lang="ts">
import { cn } from '@/lib/utils'

const props = defineProps<{ class?: string }>()
</script>

<template>
  <div v-bind="$attrs" :class="cn('glass-panel rounded-2xl text-card-foreground', props.class)">
    <slot />
  </div>
</template>
