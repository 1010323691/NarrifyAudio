<script setup lang="ts">
import { computed } from 'vue'

export interface TipRow { color?: string; name: string; value: string }
const props = defineProps<{ x: number; y: number; width: number; height: number; title?: string; rows: TipRow[] }>()
const style = computed(() => ({
  left: `${props.x}px`,
  top: `${Math.max(16, Math.min(props.y, props.height - 16))}px`,
}))
</script>

<template>
  <div class="vc-tip" :class="{ 'is-flip': x > width / 2 }" :style="style" role="presentation">
    <div v-if="title" class="vc-tip__title">{{ title }}</div>
    <div v-for="row in rows" :key="row.name" class="vc-tip__row">
      <i v-if="row.color" :style="{ background: row.color }" />
      <span>{{ row.name }}</span>
      <strong>{{ row.value }}</strong>
    </div>
  </div>
</template>
