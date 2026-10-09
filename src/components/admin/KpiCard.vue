<script setup lang="ts">
import { computed, type Component } from 'vue'
import { ArrowDownRight, ArrowUpRight, Minus } from 'lucide-vue-next'
import AdminChart from './charts/AdminChart.vue'
import { sparklineOption } from './charts/options'
import type { ChartTokens } from './charts/tokens'

const props = defineProps<{
  label: string
  value: string
  icon?: Component
  /** Accent hue index into the categorical palette (icon chip + sparkline). */
  accent?: number
  hint?: string
  delta?: { text: string; direction: 'up' | 'down' | 'flat' } | null
  /** Whether an increase is good (green) or bad (red); neutral when omitted. */
  upIsGood?: boolean
  trend?: (number | null)[]
  tone?: 'default' | 'danger' | 'warning'
}>()
const deltaTone = computed(() => {
  if (!props.delta || props.delta.direction === 'flat' || props.upIsGood === undefined) return 'neutral'
  return (props.delta.direction === 'up') === props.upIsGood ? 'good' : 'bad'
})
const spark = computed(() => (tokens: ChartTokens) => sparklineOption({
  data: props.trend ?? [], color: tokens.series[props.accent ?? 0],
}))
</script>

<template>
  <div class="kpi-card" :class="tone ? `is-${tone}` : ''">
    <div class="kpi-card__top">
      <span class="kpi-card__label">{{ label }}</span>
      <span v-if="icon" class="kpi-card__icon" :data-accent="accent ?? 0"><component :is="icon" class="h-4 w-4" /></span>
    </div>
    <strong class="kpi-card__value">{{ value }}</strong>
    <div class="kpi-card__meta">
      <span v-if="delta" class="kpi-delta" :class="`is-${deltaTone}`">
        <ArrowUpRight v-if="delta.direction === 'up'" class="h-3 w-3" /><ArrowDownRight v-else-if="delta.direction === 'down'" class="h-3 w-3" /><Minus v-else class="h-3 w-3" />{{ delta.text }}
      </span>
      <span v-if="hint" class="kpi-card__hint">{{ hint }}</span>
    </div>
    <AdminChart v-if="trend && trend.length > 1" class="kpi-card__spark" :option="spark" :height="36" :label="`${label} 趋势`" />
  </div>
</template>
