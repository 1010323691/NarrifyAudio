<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ value: number | null | undefined; max: number | null | undefined; label?: string; warn?: number; critical?: number }>()
const ratio = computed(() => props.value == null || !props.max ? 0 : Math.max(0, Math.min(1, props.value / props.max)))
const level = computed(() => props.critical != null && ratio.value >= props.critical ? 'critical' : props.warn != null && ratio.value >= props.warn ? 'warn' : 'ok')
</script>

<template>
  <div class="meter" role="meter" :aria-valuenow="value ?? 0" :aria-valuemin="0" :aria-valuemax="max ?? 0" :aria-label="label">
    <div class="meter__track"><div class="meter__fill" :class="`is-${level}`" :style="{ width: `${ratio * 100}%` }" /></div>
    <span v-if="label" class="meter__label">{{ label }}</span>
  </div>
</template>
