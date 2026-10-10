<script setup lang="ts">
import { computed, ref } from 'vue'
import type { BarSpec } from './options'
import { niceTicks, roundedRect, textWidth, truncate } from './scale'
import { pointerIn, useChartSize } from './useChartSize'
import ChartTooltip from './ChartTooltip.vue'

const props = defineProps<{ spec: BarSpec }>()
const plot = ref<HTMLElement | null>(null)
const { width, height } = useChartSize(plot)
const hover = ref<{ index: number; x: number; y: number } | null>(null)
const hidden = ref<string[]>([])
const seriesList = computed(() => props.spec.series.filter(item => !hidden.value.includes(item.name)))
function toggle(name: string) {
  hidden.value = hidden.value.includes(name) ? hidden.value.filter(item => item !== name) : [...hidden.value, name]
}

const horizontal = computed(() => !!props.spec.horizontal)
const showLegend = computed(() => props.spec.series.length > 1)

const totals = computed(() => props.spec.categories.map((_, ci) => {
  const stackTotals = new Map<string, number>()
  let max = 0
  seriesList.value.forEach(item => {
    const value = item.data[ci] ?? 0
    if (item.stack) {
      const sum = (stackTotals.get(item.stack) ?? 0) + value
      stackTotals.set(item.stack, sum)
      max = Math.max(max, sum)
    } else max = Math.max(max, value)
  })
  return max
}))
const axis = computed(() => niceTicks(0, Math.max(1, ...totals.value), { integer: true }))
const tickLabels = computed(() => axis.value.ticks.map(tick => props.spec.format(tick)))

const labelWidth = computed(() => {
  if (!horizontal.value) return 0
  return Math.min(132, Math.max(24, ...props.spec.categories.map(name => textWidth(name, 11)))) + 10
})
const margin = computed(() => horizontal.value
  ? { left: labelWidth.value, right: props.spec.labels ? 56 : 14, top: 6, bottom: 20 }
  : { left: Math.max(28, ...tickLabels.value.map(label => textWidth(label, 11))) + 10, right: 12, top: 8, bottom: 22 })
const pw = computed(() => Math.max(0, width.value - margin.value.left - margin.value.right))
const ph = computed(() => Math.max(0, height.value - margin.value.top - margin.value.bottom))

const count = computed(() => props.spec.categories.length || 1)
const band = computed(() => (horizontal.value ? ph.value : pw.value) / count.value)
const slots = computed(() => {
  const keys: string[] = []
  seriesList.value.forEach((item, si) => {
    const key = item.stack ?? `#${si}`
    if (!keys.includes(key)) keys.push(key)
  })
  return keys
})
const thickness = computed(() => Math.max(2, Math.min(24, (band.value * 0.7) / (slots.value.length || 1))))
const valueOf = (value: number) => {
  const span = axis.value.max - axis.value.min || 1
  const frac = (value - axis.value.min) / span
  return horizontal.value ? margin.value.left + frac * pw.value : margin.value.top + ph.value - frac * ph.value
}

const bars = computed(() => {
  const out: { key: string; d: string; color: string; sep: boolean; label?: { x: number; y: number; text: string; anchor: string } }[] = []
  const last = new Map<string, number>()
  seriesList.value.forEach((item, si) => { if (item.stack) last.set(item.stack, si) })
  props.spec.categories.forEach((_, ci) => {
    const running = new Map<string, number>()
    seriesList.value.forEach((item, si) => {
      const value = item.data[ci]
      if (value == null) return
      const key = item.stack ?? `#${si}`
      const slot = slots.value.indexOf(key)
      const base = item.stack ? running.get(key) ?? 0 : 0
      if (item.stack) running.set(key, base + value)
      const offset = (slot - (slots.value.length - 1) / 2) * thickness.value
      const mid = (horizontal.value ? margin.value.top : margin.value.left) + band.value * (ci + 0.5) + offset
      const a = valueOf(base)
      const b = valueOf(base + value)
      const end = !item.stack || last.get(item.stack) === si
      const len = Math.abs(a - b)
      if (len < 0.5) return
      const d = horizontal.value
        ? roundedRect(Math.min(a, b), mid - thickness.value / 2, len, thickness.value, end ? { tr: 4, br: 4 } : {})
        : roundedRect(mid - thickness.value / 2, Math.min(a, b), thickness.value, len, end ? { tl: 4, tr: 4 } : {})
      out.push({
        key: `${si}-${ci}`, d, color: item.color, sep: !!item.stack,
        label: props.spec.labels && !item.stack ? {
          text: props.spec.format(value),
          x: horizontal.value ? b + 6 : mid,
          y: horizontal.value ? mid : b - 5,
          anchor: horizontal.value ? 'start' : 'middle',
        } : undefined,
      })
    })
  })
  return out
})

const labelStep = computed(() => Math.max(1, Math.ceil(Math.max(0, ...props.spec.categories.map(name => textWidth(name, 11))) / Math.max(band.value, 1))))
const catLabels = computed(() => props.spec.categories.map((name, ci) => {
  const center = (horizontal.value ? margin.value.top : margin.value.left) + band.value * (ci + 0.5)
  const max = horizontal.value ? labelWidth.value - 10 : Math.max(0, band.value - 4)
  const text = truncate(name, max)
  return { name, text, center, show: horizontal.value || ci % labelStep.value === 0 }
}))

function move(event: PointerEvent) {
  const pos = pointerIn(event, plot.value)
  const coord = horizontal.value ? pos.y - margin.value.top : pos.x - margin.value.left
  const index = Math.max(0, Math.min(count.value - 1, Math.floor(coord / (band.value || 1))))
  hover.value = { index, x: pos.x, y: pos.y }
}
const tip = computed(() => hover.value ? seriesList.value.map(item => {
  const value = item.data[hover.value!.index]
  return { color: item.color, name: item.name, value: value == null ? '—' : props.spec.format(value) }
}) : [])
const bandRect = computed(() => {
  if (!hover.value) return null
  const start = (horizontal.value ? margin.value.top : margin.value.left) + band.value * hover.value.index
  return horizontal.value
    ? { x: margin.value.left, y: start, w: pw.value, h: band.value }
    : { x: start, y: margin.value.top, w: band.value, h: ph.value }
})
</script>

<template>
  <div class="vc">
    <div v-if="showLegend" class="vc-legend">
      <button v-for="item in spec.series" :key="item.name" type="button" :class="{ 'is-off': hidden.includes(item.name) }" :aria-pressed="!hidden.includes(item.name)" @click="toggle(item.name)"><i :style="{ background: item.color }" />{{ item.name }}</button>
    </div>
    <div ref="plot" class="vc-plot">
      <svg v-if="width > 8 && height > 8" :width="width" :height="height" :viewBox="`0 0 ${width} ${height}`" aria-hidden="true">
        <g class="vc-grid">
          <template v-for="(tick, index) in axis.ticks" :key="tick">
            <template v-if="horizontal">
              <line :x1="valueOf(tick)" :x2="valueOf(tick)" :y1="margin.top" :y2="margin.top + ph" :class="{ 'is-base': index === 0 }" />
              <text :x="valueOf(tick)" :y="height - 6" text-anchor="middle">{{ tickLabels[index] }}</text>
            </template>
            <template v-else>
              <line :x1="margin.left" :x2="width - margin.right" :y1="valueOf(tick)" :y2="valueOf(tick)" :class="{ 'is-base': index === 0 }" />
              <text :x="margin.left - 8" :y="valueOf(tick)" text-anchor="end" dominant-baseline="central">{{ tickLabels[index] }}</text>
            </template>
          </template>
        </g>
        <g class="vc-xaxis">
          <template v-for="label in catLabels" :key="label.name">
            <text v-if="horizontal" :x="margin.left - 8" :y="label.center" text-anchor="end" dominant-baseline="central" class="is-cat"><title>{{ label.name }}</title>{{ label.text }}</text>
            <text v-else-if="label.show" :x="label.center" :y="height - 6" text-anchor="middle"><title>{{ label.name }}</title>{{ label.text }}</text>
          </template>
        </g>
        <rect v-if="bandRect" class="vc-band-rect" :x="bandRect.x" :y="bandRect.y" :width="bandRect.w" :height="bandRect.h" />
        <g>
          <path v-for="bar in bars" :key="bar.key" :d="bar.d" :style="{ fill: bar.color }" :class="{ 'vc-bar--sep': bar.sep }" />
          <text v-for="bar in bars.filter(item => item.label)" :key="`l${bar.key}`" class="vc-value" :x="bar.label!.x" :y="bar.label!.y" :text-anchor="bar.label!.anchor" :dominant-baseline="horizontal ? 'central' : undefined">{{ bar.label!.text }}</text>
        </g>
        <rect class="vc-hit" :x="margin.left" :y="margin.top" :width="pw" :height="ph" @pointermove="move" @pointerdown="move" @pointerleave="hover = null" />
      </svg>
      <ChartTooltip v-if="hover" :x="hover.x" :y="hover.y" :width="width" :height="height" :title="spec.categories[hover.index]" :rows="tip" />
      <div v-if="!spec.categories.length && width > 8" class="vc-empty">暂无数据</div>
    </div>
  </div>
</template>
