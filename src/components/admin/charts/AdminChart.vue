<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, shallowRef, watch, type WatchStopHandle } from 'vue'
import { echarts, type EChartsCoreOption } from './echarts'
import { chartTokens, type ChartTokens } from './tokens'

const props = withDefaults(defineProps<{
  /** Builds the option from the current light/dark tokens, so a theme switch re-colours in place. */
  option: (tokens: ChartTokens) => EChartsCoreOption
  label: string
  height?: number
}>(), { height: 240 })

const el = ref<HTMLDivElement | null>(null)
const chart = shallowRef<ReturnType<typeof echarts.init> | null>(null)
const dark = ref(typeof document !== 'undefined' && document.documentElement.classList.contains('dark'))
const reducedMotion = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
let resizeObserver: ResizeObserver | null = null
let themeObserver: MutationObserver | null = null
let stopRender: WatchStopHandle | null = null

// The option is built inside the watcher's getter, so every reactive value the
// builder reads (the view's data, the theme) re-renders the chart; setOption
// itself runs untracked in the callback.
function build() {
  return props.option(chartTokens(dark.value)) as Record<string, unknown>
}
function render(option: Record<string, unknown>) {
  chart.value?.setOption({ ...option, backgroundColor: 'transparent', animation: reducedMotion ? false : (option.animation as boolean | undefined) ?? true }, { notMerge: true })
}

onMounted(() => {
  if (!el.value) return
  chart.value = echarts.init(el.value, undefined, { renderer: 'canvas' })
  stopRender = watch(build, render, { immediate: true })
  resizeObserver = new ResizeObserver(() => chart.value?.resize())
  resizeObserver.observe(el.value)
  themeObserver = new MutationObserver(() => {
    dark.value = document.documentElement.classList.contains('dark')
  })
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
})
onBeforeUnmount(() => {
  stopRender?.()
  resizeObserver?.disconnect()
  themeObserver?.disconnect()
  chart.value?.dispose()
  chart.value = null
})
</script>

<template>
  <div ref="el" class="admin-chart" role="img" :aria-label="label" :style="{ height: `${height}px` }" />
</template>
