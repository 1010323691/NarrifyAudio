<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  variant?: 'default' | 'secondary' | 'success' | 'warning' | 'destructive' | 'outline'
  mixed?: boolean
}>(), { variant: 'secondary', mixed: false })

// Match the chapter preview's compact text statuses without badge backgrounds.
const toneClass = computed(() => props.mixed && props.variant === 'success'
  ? 'text-sky-600 dark:text-sky-400'
  : {
    default: 'text-primary',
    secondary: 'text-muted-foreground',
    success: 'text-teal-600 dark:text-teal-400',
    warning: 'text-amber-600 dark:text-amber-400',
    destructive: 'text-destructive',
    outline: 'text-muted-foreground',
  }[props.variant])
</script>

<template>
  <span class="inline-flex items-center gap-1 text-xs font-semibold leading-normal" :class="toneClass"><slot /></span>
</template>
