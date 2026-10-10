<script setup lang="ts">
import { computed, ref } from 'vue'
import type { DonutSpec } from './options'
import { donutSlices } from './scale'
import { pointerIn, useChartSize } from './useChartSize'
import ChartTooltip from './ChartTooltip.vue'

const props = defineProps<{ spec: DonutSpec }>()
const plot = ref<HTMLElement | null>(null)
const { width, height } = useChartSize(plot)
const active = ref<number | null>(null)
const pos = ref({ x: 0, y: 0 })

const R = 46
const CIRC = 2 * Math.PI * R
const sum = computed(() => props.spec.data.reduce((total, item) => total + item.value, 0))
const slices = computed(() => donutSlices(props.spec.data.map(item => item.value), CIRC, 1.5))
const size = computed(() => Math.max(0, Math.min(height.value, width.value * 0.52)))
const percent = (value: number) => (sum.value ? (value / sum.value) * 100 : 0).toFixed(1)

function enter(index: number, event: PointerEvent) {
  active.value = index
  pos.value = pointerIn(event, plot.value)
}
const tip = computed(() => {
  const item = active.value == null ? null : props.spec.data[active.value]
  return item ? [{ color: item.color, name: item.name, value: `${props.spec.format(item.value)} · ${percent(item.value)}%` }] : []
})
</script>

<template>
  <div ref="plot" class="vc-plot vc-donut">
    <template v-if="width > 8 && height > 8">
      <svg class="vc-donut__ring" :width="size" :height="size" viewBox="0 0 120 120" aria-hidden="true">
        <circle cx="60" cy="60" :r="R" fill="none" stroke-width="14" class="vc-track" />
        <template v-if="sum > 0">
          <circle v-for="(item, index) in spec.data" :key="item.name" cx="60" cy="60" :r="R" fill="none" stroke-width="14"
                  :style="{ stroke: item.color }" :opacity="active != null && active !== index ? 0.35 : 1"
                  :stroke-dasharray="`${slices[index].length} ${CIRC - slices[index].length}`" :stroke-dashoffset="-slices[index].offset"
                  transform="rotate(-90 60 60)" class="vc-seg"
                  @pointermove="enter(index, $event)" @pointerleave="active = null" />
        </template>
        <text x="60" y="58" text-anchor="middle" class="vc-donut__total">{{ sum > 0 ? (spec.total ?? spec.format(sum)) : '—' }}</text>
        <text x="60" y="74" text-anchor="middle" class="vc-donut__sub">{{ sum > 0 ? spec.title : '暂无数据' }}</text>
      </svg>
      <ul class="vc-donut__legend">
        <li v-for="(item, index) in spec.data" :key="item.name" :class="{ 'is-dim': active != null && active !== index }"
            @pointermove="enter(index, $event)" @pointerleave="active = null">
          <i :style="{ background: item.color }" /><span>{{ item.name }}</span><strong>{{ spec.format(item.value) }}</strong>
        </li>
      </ul>
      <ChartTooltip v-if="active != null && tip.length" :x="pos.x" :y="pos.y" :width="width" :height="height" :rows="tip" />
    </template>
  </div>
</template>
