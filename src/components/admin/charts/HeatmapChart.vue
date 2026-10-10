<script setup lang="ts">
import { computed, ref } from 'vue'
import type { HeatmapSpec } from './options'
import { textWidth } from './scale'
import { pointerIn, useChartSize } from './useChartSize'
import ChartTooltip from './ChartTooltip.vue'

const props = defineProps<{ spec: HeatmapSpec }>()
const plot = ref<HTMLElement | null>(null)
const { width, height } = useChartSize(plot)
const hover = ref<{ cell: number; x: number; y: number } | null>(null)

const max = computed(() => Math.max(1, ...props.spec.data.map(item => item[2])))
const margin = computed(() => ({
  left: Math.max(24, ...props.spec.yLabels.map(label => textWidth(label, 11))) + 10, right: 4, top: 4, bottom: 44,
}))
const cols = computed(() => props.spec.xLabels.length || 1)
const rows = computed(() => props.spec.yLabels.length || 1)
const cw = computed(() => Math.max(0, (width.value - margin.value.left - margin.value.right) / cols.value))
const ch = computed(() => Math.max(0, (height.value - margin.value.top - margin.value.bottom) / rows.value))
const cells = computed(() => props.spec.data.map(([x, y, value], index) => ({
  index, x: margin.value.left + x * cw.value, y: margin.value.top + y * ch.value, value, xi: x, yi: y,
  fill: `color-mix(in srgb, var(--viz-1) ${Math.round(8 + (value / max.value) * 92)}%, hsl(var(--card)))`,
})))
const step = computed(() => Math.max(1, Math.ceil(26 / Math.max(cw.value, 1))))
const legend = [8, 30, 52, 74, 100].map(pct => `color-mix(in srgb, var(--viz-1) ${pct}%, hsl(var(--card)))`)

function enter(cell: number, event: PointerEvent) {
  hover.value = { cell, ...pointerIn(event, plot.value) }
}
const tip = computed(() => {
  const cell = hover.value ? cells.value[hover.value.cell] : null
  return cell ? { title: `${props.spec.yLabels[cell.yi]} ${props.spec.xLabels[cell.xi]}:00`, rows: [{ color: cell.fill, name: '提交任务', value: props.spec.format(cell.value) }] } : null
})
</script>

<template>
  <div ref="plot" class="vc-plot">
    <svg v-if="width > 8 && height > 8" :width="width" :height="height" :viewBox="`0 0 ${width} ${height}`" aria-hidden="true">
      <g class="vc-xaxis">
        <text v-for="(label, index) in spec.xLabels" v-show="index % step === 0" :key="`x${index}`" :x="margin.left + (index + 0.5) * cw" :y="margin.top + rows * ch + 14" text-anchor="middle" class="is-small">{{ label }}</text>
        <text v-for="(label, index) in spec.yLabels" :key="`y${index}`" :x="margin.left - 8" :y="margin.top + (index + 0.5) * ch" text-anchor="end" dominant-baseline="central" class="is-cat">{{ label }}</text>
      </g>
      <rect v-for="cell in cells" :key="cell.index" :x="cell.x + 1" :y="cell.y + 1" :width="Math.max(0, cw - 2)" :height="Math.max(0, ch - 2)" rx="3"
            :style="{ fill: cell.fill }" class="vc-cell" :class="{ 'is-active': hover?.cell === cell.index }"
            @pointermove="enter(cell.index, $event)" @pointerleave="hover = null" />
      <g class="vc-heat-legend" :transform="`translate(${Math.max(margin.left + 20, width - 5 * 14 - 44)},${height - 14})`">
        <text x="-4" y="0" text-anchor="end" dominant-baseline="central">少</text>
        <rect v-for="(fill, index) in legend" :key="index" :x="index * 14" y="-5" width="12" height="10" rx="2" :style="{ fill }" />
        <text :x="5 * 14 + 2" y="0" dominant-baseline="central">{{ spec.format(max) }}</text>
      </g>
    </svg>
    <ChartTooltip v-if="hover && tip" :x="hover.x" :y="hover.y" :width="width" :height="height" :title="tip.title" :rows="tip.rows" />
  </div>
</template>
