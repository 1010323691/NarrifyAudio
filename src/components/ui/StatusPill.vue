<script setup lang="ts">
import { computed } from 'vue'

type Tone = 'positive' | 'warning' | 'negative' | 'neutral'

interface Props {
  label: string
  tone?: Tone
  ariaLabel?: string
}

const props = withDefaults(defineProps<Props>(), { tone: 'neutral' })

const toneClass = computed(() => ({
  positive: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  warning: 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300',
  negative: 'border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-300',
  neutral: 'border-border bg-muted/60 text-muted-foreground',
}[props.tone]))

const dotClass = computed(() => ({
  positive: 'bg-emerald-500 dark:bg-emerald-400',
  warning: 'bg-amber-500 dark:bg-amber-400',
  negative: 'bg-red-500 dark:bg-red-400',
  neutral: 'bg-muted-foreground/60',
}[props.tone]))
</script>

<template>
  <span
    class="inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] font-semibold leading-none tracking-wide"
    :class="toneClass"
    role="status"
    :aria-label="props.ariaLabel || props.label"
  >
    <span class="h-1.5 w-1.5 shrink-0 rounded-full" :class="dotClass" aria-hidden="true" />
    {{ props.label }}
  </span>
</template>
