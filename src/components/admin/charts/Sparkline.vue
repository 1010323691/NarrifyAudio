<script setup lang="ts">
import { computed } from 'vue'
import { sparkPaths } from './scale'

// Design-doc sparkline: stretched 100×32 box, non-scaling stroke, currentColor.
const props = defineProps<{ data: (number | null)[]; color: string; max?: number }>()
const paths = computed(() => sparkPaths(props.data, props.max))
</script>

<template>
  <svg class="vc-spark" viewBox="0 0 100 32" preserveAspectRatio="none" aria-hidden="true" :style="{ color }">
    <path :d="paths.area" fill="currentColor" opacity=".12" />
    <path :d="paths.line" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke" />
  </svg>
</template>
