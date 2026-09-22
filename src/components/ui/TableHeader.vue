<script setup lang="ts">
import { cn } from '@/lib/utils'

const props = defineProps<{ class?: string }>()
</script>

<template>
  <thead :class="cn('[&_tr]:border-b [&_th]:bg-muted/60 [&_th]:text-muted-foreground', props.class)"><slot /></thead>
</template>
