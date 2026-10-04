<script setup lang="ts">
import { computed } from 'vue'
import WorkbenchStatus from './WorkbenchStatus.vue'

type Tone = 'positive' | 'warning' | 'negative' | 'neutral'
const props = withDefaults(defineProps<{
  label: string
  tone?: Tone
  ariaLabel?: string
}>(), { tone: 'neutral' })
const variant = computed(() => ({
  positive: 'success', warning: 'warning', negative: 'destructive', neutral: 'secondary',
} as const)[props.tone])
</script>

<template>
  <WorkbenchStatus :variant="variant" role="status" :aria-label="props.ariaLabel || props.label">
    {{ props.label }}
  </WorkbenchStatus>
</template>
