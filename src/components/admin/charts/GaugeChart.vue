<script setup lang="ts">
import { computed } from 'vue'
import type { GaugeSpec } from './options'
import { GAUGE_ARC, GAUGE_CIRC, gaugeDash } from './scale'

const props = defineProps<{ spec: GaugeSpec }>()
const ratio = computed(() => props.spec.value == null || !props.spec.max ? 0 : Math.min(1, props.spec.value / props.spec.max))
const color = computed(() => {
  const { critical, warn } = props.spec
  if (critical != null && ratio.value >= critical) return 'var(--status-critical)'
  if (warn != null && ratio.value >= warn) return 'var(--status-serious)'
  return 'var(--viz-1)'
})
// A zero-length round cap still paints a dot; hide the fill below ~3 units of arc.
const visible = computed(() => GAUGE_ARC * ratio.value >= 3)
</script>

<template>
  <div class="vc-plot vc-gauge">
    <svg viewBox="0 0 120 120" aria-hidden="true">
      <circle cx="60" cy="60" r="50" fill="none" stroke-width="10" stroke-linecap="round" class="vc-track"
              :stroke-dasharray="`${GAUGE_ARC.toFixed(1)} ${GAUGE_CIRC.toFixed(1)}`" transform="rotate(135 60 60)" />
      <circle cx="60" cy="60" r="50" fill="none" stroke-width="10" stroke-linecap="round" class="vc-gauge__fill"
              :style="{ stroke: color, opacity: visible ? 1 : 0 }" :stroke-dasharray="gaugeDash(ratio)" transform="rotate(135 60 60)" />
      <text x="60" y="64" text-anchor="middle" class="vc-gauge__value">{{ spec.value == null ? '–' : spec.format(spec.value) }}</text>
      <text x="60" y="84" text-anchor="middle" class="vc-gauge__label">{{ spec.label }}</text>
    </svg>
  </div>
</template>
