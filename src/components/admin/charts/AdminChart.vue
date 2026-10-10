<script setup lang="ts">
import { computed } from 'vue'
import type { ChartSpec } from './options'
import { chartTokens, type ChartTokens } from './tokens'
import TimeSeriesChart from './TimeSeriesChart.vue'
import BarChart from './BarChart.vue'
import DonutChart from './DonutChart.vue'
import GaugeChart from './GaugeChart.vue'
import HeatmapChart from './HeatmapChart.vue'
import Sparkline from './Sparkline.vue'

const props = withDefaults(defineProps<{
  /** Builds the chart spec from the colour tokens; re-evaluated when the view's data changes. */
  option: (tokens: ChartTokens) => ChartSpec
  label: string
  height?: number
}>(), { height: 240 })

const spec = computed(() => props.option(chartTokens()))
</script>

<template>
  <div class="admin-chart" role="group" :aria-label="label" :style="{ height: `${height}px` }">
    <TimeSeriesChart v-if="spec.kind === 'time'" :spec="spec" />
    <BarChart v-else-if="spec.kind === 'bar'" :spec="spec" />
    <DonutChart v-else-if="spec.kind === 'donut'" :spec="spec" />
    <GaugeChart v-else-if="spec.kind === 'gauge'" :spec="spec" />
    <HeatmapChart v-else-if="spec.kind === 'heatmap'" :spec="spec" />
    <Sparkline v-else :data="spec.data" :color="spec.color" />
  </div>
</template>
