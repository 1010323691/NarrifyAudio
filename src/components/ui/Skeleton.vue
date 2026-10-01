<script setup lang="ts">
import { cn } from '@/lib/utils'

const props = defineProps<{ class?: string }>()
</script>

<template>
  <div aria-hidden="true" :class="cn('animate-pulse rounded-md bg-muted motion-reduce:animate-none', props.class)" />
</template>
