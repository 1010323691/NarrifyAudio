<script setup lang="ts">
import { cn } from '@/lib/utils'

// `value` is a fraction in [0, 1] (matches the backend's task.progress).
const props = defineProps<{ value?: number; class?: string; indicatorClass?: string }>()

function pct(): string {
  const v = props.value ?? 0
  return (Math.max(0, Math.min(1, v)) * 100).toFixed(1) + '%'
}
</script>

<template>
  <div :class="cn('relative h-2.5 w-full overflow-hidden rounded-full bg-secondary', props.class)">
    <div
      :class="cn('h-full rounded-full bg-gradient-to-r from-indigo-400 to-primary transition-all', props.indicatorClass)"
      :style="{ width: pct() }"
      role="progressbar"
      :aria-valuenow="Math.round((props.value ?? 0) * 100)"
      aria-valuemin="0"
      aria-valuemax="100"
    />
  </div>
</template>
