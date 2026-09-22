<script setup lang="ts">
import { cn } from '@/lib/utils'

const props = defineProps<{ class?: string }>()
</script>

<template>
  <div v-bind="$attrs" :class="cn('rounded-2xl border bg-card text-card-foreground shadow-[0_1px_2px_hsl(222_47%_11%_/_0.03)]', props.class)">
    <slot />
  </div>
</template>
