<script setup lang="ts">
import { cn } from '@/lib/utils'

const props = defineProps<{ class?: string }>()
</script>

<template>
  <!--
    Single scroll element: the height/max-height class (e.g. max-h-72) and
    overflow-auto must live on the SAME box. The previous two-layer form put
    overflow on an inner div whose `h-full` resolved to `auto` against a
    max-height-only parent, so max-height usages never scrolled and their
    content overflowed the box. One element with height/max-height + overflow
    auto handles both fixed (h-…) and capped (max-h-…) cases correctly.
  -->
  <div :class="cn('relative overflow-auto', props.class)">
    <slot />
  </div>
</template>
