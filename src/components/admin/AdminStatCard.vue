<script setup lang="ts">
defineProps<{ label: string; valueClass?: string }>()
</script>

<template>
  <div class="metric">
    <span>{{ label }}</span>
    <strong :class="valueClass"><slot name="value" /></strong>
    <small v-if="$slots.default"><slot /></small>
  </div>
</template>
