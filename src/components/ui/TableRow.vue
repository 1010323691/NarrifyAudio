<script setup lang="ts">
import { cn } from '@/lib/utils'

const props = defineProps<{ class?: string }>()
</script>

<template>
  <tr
    v-bind="$attrs"
    :class="cn('border-b transition-colors hover:bg-muted/40 data-[state=selected]:bg-accent/60', props.class)"
  >
    <slot />
  </tr>
</template>
