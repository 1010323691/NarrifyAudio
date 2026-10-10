<script setup lang="ts">
import { computed, ref } from 'vue'
import type { TimeSpec } from './options'
import { areaPath, linePath, nearestIndex, niceTicks, roundedRect, segments, textWidth, timeLabel, tooltipTime } from './scale'
import { pointerIn, useChartSize } from './useChartSize'
import ChartTooltip from './ChartTooltip.vue'

const props = defineProps<{ spec: TimeSpec }>()
const plot = ref<HTMLElement | null>(null)
const { width, height } = useChartSize(plot)
const hover = ref<{ index: number; x: number; y: number } | null>(null)
const hidden = ref<string[]>([])
const seriesList = computed(() => props.spec.series.filter(item => !hidden.value.includes(item.name)))
function toggle(name: string) {
  hidden.value = hidden.value.includes(name) ? hidden.value.filter(item => item !== name) : [...hidden.value, name]
}

const showLegend = computed(() => props.spec.series.length > 1)
const times = computed(() => {
  const set = new Set<number>()
  for (const item of seriesList.value) for (const [time] of item.data) set.add(Date.parse(time))
  return [...set].filter(Number.isFinite).sort((a, b) => a - b)
})
const lookups = computed(() => seriesList.value.map(item => new Map(item.data.map(([time, value]) => [Date.parse(time), value] as const))))
const raw = computed(() => lookups.value.map(map => times.value.map(time => map.get(time) ?? null)))

// Stack tops/bases per series and time (series stacked within the same `stack` key).
const stacked = computed(() => {
  const tops: (number | null)[][] = seriesList.value.map(() => [])
  const bases: number[][] = seriesList.value.map(() => [])
  times.value.forEach((_, ti) => {
    const running = new Map<string, number>()
    seriesList.value.forEach((item, si) => {
      const value = raw.value[si][ti]
      const key = item.stack
      const base = key ? running.get(key) ?? 0 : 0
      bases[si][ti] = base
      tops[si][ti] = value == null ? null : base + value
      if (key && value != null) running.set(key, base + value)
    })
  })
  return { tops, bases }
})

const hasBars = computed(() => seriesList.value.some(item => item.type === 'bar'))
const domainY = computed(() => {
  const tops = stacked.value.tops.flat().filter((value): value is number => value != null)
  if (props.spec.markLine) tops.push(props.spec.markLine.value)
  let lo = 0
  let hi = tops.length ? Math.max(...tops) : 1
  if (props.spec.fit && tops.length) {
    lo = Math.min(...tops)
    const pad = (hi - lo) * 0.1 || Math.abs(hi) * 0.1 || 1
    lo = Math.max(0, lo - pad)
    hi += pad
  }
  const fixedMax = props.spec.max != null
  if (fixedMax) hi = props.spec.max as number
  return niceTicks(lo, hi, { integer: !props.spec.decimals, fixedMax })
})
const yLabels = computed(() => domainY.value.ticks.map(tick => props.spec.format(tick)))
const margin = computed(() => ({
  left: Math.max(28, ...yLabels.value.map(label => textWidth(label, 11))) + 10,
  right: 12, top: 8, bottom: 22,
}))
const pw = computed(() => Math.max(0, width.value - margin.value.left - margin.value.right))
const ph = computed(() => Math.max(0, height.value - margin.value.top - margin.value.bottom))

const gap = computed(() => {
  let min = Infinity
  for (let i = 1; i < times.value.length; i++) min = Math.min(min, times.value[i] - times.value[i - 1])
  return Number.isFinite(min) ? min : 60_000
})
const domainX = computed(() => {
  const first = times.value[0] ?? 0
  const last = times.value[times.value.length - 1] ?? first + 1
  const pad = hasBars.value || times.value.length < 2 ? gap.value / 2 : 0
  return [first - pad, Math.max(last + pad, first - pad + 1)] as const
})
const xOf = (time: number) => margin.value.left + ((time - domainX.value[0]) / (domainX.value[1] - domainX.value[0])) * pw.value
const yOf = (value: number) => {
  const { min, max } = domainY.value
  const clamped = Math.max(min, Math.min(max, value))
  return margin.value.top + ph.value - ((clamped - min) / (max - min || 1)) * ph.value
}

const xTicks = computed(() => {
  if (!times.value.length || pw.value <= 0) return []
  const count = Math.max(2, Math.min(8, Math.floor(pw.value / 96)))
  const [d0, d1] = domainX.value
  const out: { x: number; label: string; anchor: string }[] = []
  for (let i = 0; i < count; i++) {
    const t = d0 + ((d1 - d0) * i) / (count - 1)
    const label = timeLabel(hasBars.value ? Math.round(t) : t, props.spec.scale)
    if (out.length && out[out.length - 1].label === label) continue
    out.push({ x: xOf(t), label, anchor: i === 0 ? 'start' : i === count - 1 ? 'end' : 'middle' })
  }
  return out
})

// Bars: stack groups share a slot, un-stacked bars sit side by side.
const barSlots = computed(() => {
  const keys: string[] = []
  seriesList.value.forEach((item, si) => {
    if (item.type !== 'bar') return
    const key = item.stack ?? `#${si}`
    if (!keys.includes(key)) keys.push(key)
  })
  return keys
})
const barWidth = computed(() => {
  const slots = barSlots.value.length || 1
  const step = times.value.length > 1 ? (gap.value / (domainX.value[1] - domainX.value[0])) * pw.value : pw.value / 3
  return Math.max(2, Math.min(24, (step * 0.72) / slots))
})
const stackLast = computed(() => {
  const last = new Map<string, number>()
  seriesList.value.forEach((item, si) => { if (item.type === 'bar' && item.stack) last.set(item.stack, si) })
  return last
})
const bars = computed(() => {
  const out: { key: string; d: string; color: string; stroke: boolean }[] = []
  const w = barWidth.value
  seriesList.value.forEach((item, si) => {
    if (item.type !== 'bar') return
    const slot = barSlots.value.indexOf(item.stack ?? `#${si}`)
    const offset = (slot - (barSlots.value.length - 1) / 2) * w
    const isEnd = !item.stack || stackLast.value.get(item.stack) === si
    times.value.forEach((time, ti) => {
      const top = stacked.value.tops[si][ti]
      if (top == null) return
      const y1 = yOf(top)
      const y0 = yOf(stacked.value.bases[si][ti])
      if (y0 - y1 < 0.5) return
      out.push({
        key: `${si}-${ti}`, color: item.color, stroke: !!item.stack,
        d: roundedRect(xOf(time) + offset - w / 2, y1, w, y0 - y1, isEnd ? { tl: 4, tr: 4 } : {}),
      })
    })
  })
  return out
})

const lines = computed(() => seriesList.value.flatMap((item, si) => {
  if (item.type === 'bar') return []
  const pts = times.value.map((time, ti) => {
    const top = stacked.value.tops[si][ti]
    return { x: xOf(time), y: top == null ? null : yOf(top), base: yOf(stacked.value.bases[si][ti] || domainY.value.min) }
  })
  return segments(pts).map((segment, index) => {
    return {
      key: `${si}-${index}`, color: item.color, dashed: !!item.dashed, single: segment.length === 1, point: segment[0],
      d: linePath(segment), area: item.area ? areaPath(segment, segment.map(point => point.base)) : '', stacked: !!item.stack,
    }
  })
}))

const markY = computed(() => props.spec.markLine ? yOf(props.spec.markLine.value) : null)

function move(event: PointerEvent) {
  if (!times.value.length) return
  const pos = pointerIn(event, plot.value)
  const target = domainX.value[0] + ((pos.x - margin.value.left) / (pw.value || 1)) * (domainX.value[1] - domainX.value[0])
  hover.value = { index: nearestIndex(times.value, target), x: pos.x, y: pos.y }
}
const hoverTime = computed(() => hover.value ? times.value[hover.value.index] : null)
const tip = computed(() => {
  if (!hover.value || hoverTime.value == null) return []
  return seriesList.value.map((item, si) => {
    const value = raw.value[si][hover.value!.index]
    return { color: item.color, name: item.name, value: value == null ? '—' : props.spec.format(value) }
  })
})
const hoverDots = computed(() => hover.value ? seriesList.value.flatMap((item, si) => {
  const top = stacked.value.tops[si][hover.value!.index]
  return top == null || item.type === 'bar' ? [] : [{ key: si, color: item.color, y: yOf(top) }]
}) : [])
</script>

<template>
  <div class="vc">
    <div v-if="showLegend" class="vc-legend">
      <button v-for="item in spec.series" :key="item.name" type="button" :class="{ 'is-off': hidden.includes(item.name) }" :aria-pressed="!hidden.includes(item.name)" @click="toggle(item.name)"><i :style="{ background: item.color }" />{{ item.name }}</button>
    </div>
    <div ref="plot" class="vc-plot">
      <svg v-if="width > 8 && height > 8" :width="width" :height="height" :viewBox="`0 0 ${width} ${height}`" aria-hidden="true">
        <g class="vc-grid">
          <template v-for="(tick, index) in domainY.ticks" :key="tick">
            <line :x1="margin.left" :x2="width - margin.right" :y1="yOf(tick)" :y2="yOf(tick)" :class="{ 'is-base': index === 0 }" />
            <text :x="margin.left - 8" :y="yOf(tick)" text-anchor="end" dominant-baseline="central">{{ yLabels[index] }}</text>
          </template>
        </g>
        <g class="vc-xaxis">
          <text v-for="tick in xTicks" :key="tick.x" :x="tick.x" :y="height - 6" :text-anchor="tick.anchor">{{ tick.label }}</text>
        </g>
        <g v-if="hover && hasBars" class="vc-band">
          <rect :x="xOf(hoverTime!) - Math.max(barWidth * barSlots.length, 12) / 2 - 2" :y="margin.top" :width="Math.max(barWidth * barSlots.length, 12) + 4" :height="ph" />
        </g>
        <path v-for="bar in bars" :key="bar.key" :d="bar.d" :style="{ fill: bar.color }" :class="{ 'vc-bar--sep': bar.stroke }" />
        <template v-for="line in lines" :key="line.key">
          <path v-if="line.area" :d="line.area" :style="{ fill: line.color }" :opacity="line.stacked ? 0.22 : 0.1" />
          <circle v-if="line.single" :cx="line.point.x" :cy="line.point.y" r="2.5" :style="{ fill: line.color }" />
          <path v-else :d="line.d" fill="none" :style="{ stroke: line.color }" class="vc-line" :stroke-dasharray="line.dashed ? '5 4' : undefined" />
        </template>
        <g v-if="markY != null" class="vc-mark">
          <line :x1="margin.left" :x2="width - margin.right" :y1="markY" :y2="markY" />
          <text :x="width - margin.right - 2" :y="markY - 4" text-anchor="end">{{ spec.markLine!.label }}</text>
        </g>
        <g v-if="hover && hoverTime != null">
          <line v-if="!hasBars" class="vc-cross" :x1="xOf(hoverTime)" :x2="xOf(hoverTime)" :y1="margin.top" :y2="margin.top + ph" />
          <circle v-for="dot in hoverDots" :key="dot.key" :cx="xOf(hoverTime)" :cy="dot.y" r="4" class="vc-dot" :style="{ fill: dot.color }" />
        </g>
        <rect class="vc-hit" :x="margin.left" :y="margin.top" :width="pw" :height="ph" @pointermove="move" @pointerdown="move" @pointerleave="hover = null" />
      </svg>
      <ChartTooltip v-if="hover && hoverTime != null" :x="xOf(hoverTime)" :y="hover.y" :width="width" :height="height" :title="tooltipTime(hoverTime, spec.scale)" :rows="tip" />
      <div v-if="!times.length && width > 8" class="vc-empty">暂无数据</div>
    </div>
  </div>
</template>
