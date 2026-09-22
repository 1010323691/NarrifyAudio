<script setup lang="ts">
import { cn } from '@/lib/utils'

const props = defineProps<{ class?: string }>()
</script>

<template>
  <td v-bind="$attrs" :class="cn('px-3 py-3 align-middle', props.class)">
    <slot />
  </td>
</template>
