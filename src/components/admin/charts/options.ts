// Option builders: one place for the mark specs (2px lines, ≤24px bars with
// 4px rounded data-ends, 10% area washes, hairline grid, one y-axis) so every
// admin chart reads as one system.
import type { EChartsCoreOption } from './echarts'
import type { ChartTokens } from './tokens'

export type ValueFormatter = (value: number) => string
export interface TimeSeries {
  name: string
  color: string
  data: [string, number | null][]
  area?: boolean
  stack?: string
  type?: 'line' | 'bar'
  dashed?: boolean
}
export type TimeScale = 'minute' | 'hour' | 'day'

const pad = (value: number) => String(value).padStart(2, '0')
function timeLabel(value: number, scale: TimeScale): string {
  const time = new Date(value)
  if (scale === 'minute') return `${pad(time.getHours())}:${pad(time.getMinutes())}`
  if (scale === 'hour') return `${pad(time.getMonth() + 1)}-${pad(time.getDate())} ${pad(time.getHours())}:00`
  return `${pad(time.getMonth() + 1)}-${pad(time.getDate())}`
}

function tooltipTime(value: string | number | Date, scale: TimeScale): string {
  const time = new Date(value)
  const day = `${pad(time.getMonth() + 1)}-${pad(time.getDate())}`
  return scale === 'day' ? day : `${day} ${pad(time.getHours())}:${pad(time.getMinutes())}`
}

function tooltipBase(tokens: ChartTokens) {
  return {
    backgroundColor: tokens.tooltipBg,
    borderColor: tokens.tooltipBorder,
    borderWidth: 1,
    padding: [8, 12],
    textStyle: { color: tokens.text, fontSize: 12 },
    extraCssText: `box-shadow: 0 8px 24px rgba(15,20,30,${tokens.dark ? '.45' : '.12'}); border-radius: 8px;`,
    confine: true,
  }
}

function tooltipRow(tokens: ChartTokens, color: string, name: string, value: string) {
  return `<div style="display:flex;align-items:center;gap:8px;line-height:22px">`
    + `<span style="width:8px;height:8px;border-radius:2px;background:${color};flex:none"></span>`
    + `<span style="color:${tokens.textSecondary};flex:1">${name}</span>`
    + `<strong style="color:${tokens.text};font-weight:600;margin-left:16px">${value}</strong></div>`
}

function legend(tokens: ChartTokens, show: boolean) {
  return show ? {
    top: 0, left: 0, icon: 'roundRect', itemWidth: 10, itemHeight: 10, itemGap: 16,
    textStyle: { color: tokens.textSecondary, fontSize: 12 },
  } : { show: false }
}

function valueAxis(tokens: ChartTokens, formatter: ValueFormatter, max?: number) {
  return {
    type: 'value', max, minInterval: 1, splitNumber: 4,
    axisLabel: { color: tokens.muted, fontSize: 11, formatter: (value: number) => formatter(value) },
    splitLine: { lineStyle: { color: tokens.grid, width: 1 } },
    axisLine: { show: false }, axisTick: { show: false },
  }
}

export function timeSeriesOption(tokens: ChartTokens, options: {
  series: TimeSeries[]
  format: ValueFormatter
  scale: TimeScale
  max?: number
  /** Decimal y values (rates, percentages) — otherwise ticks snap to integers. */
  decimals?: boolean
  /** Fit the y range to the data instead of starting at zero (ratios that hover near a ceiling). */
  fit?: boolean
  markLine?: { value: number; label: string }
}): EChartsCoreOption {
  const showLegend = options.series.length > 1
  const yAxis = valueAxis(tokens, options.format, options.max) as Record<string, unknown>
  if (options.decimals) delete yAxis.minInterval
  if (options.fit) yAxis.scale = true
  return {
    color: options.series.map(item => item.color),
    legend: legend(tokens, showLegend),
    grid: { top: showLegend ? 36 : 12, right: 12, bottom: 4, left: 4, containLabel: true },
    tooltip: {
      ...tooltipBase(tokens), trigger: 'axis',
      axisPointer: { type: 'line', lineStyle: { color: tokens.axis, width: 1 } },
      formatter: (items: any[]) => {
        if (!items.length) return ''
        const head = `<div style="color:${tokens.muted};margin-bottom:4px">${tooltipTime(items[0].value[0], options.scale)}</div>`
        return head + items.map(item => tooltipRow(tokens, item.color, item.seriesName,
          item.value[1] == null ? '—' : options.format(Number(item.value[1])))).join('')
      },
    },
    xAxis: {
      type: 'time',
      axisLabel: { color: tokens.muted, fontSize: 11, hideOverlap: true, alignMinLabel: 'left', alignMaxLabel: 'right', formatter: (value: number) => timeLabel(value, options.scale) },
      axisLine: { lineStyle: { color: tokens.axis } }, axisTick: { show: false }, splitLine: { show: false },
    },
    yAxis,
    series: options.series.map((item, index) => {
      const bar = item.type === 'bar'
      return {
        name: item.name, type: bar ? 'bar' : 'line', data: item.data, stack: item.stack,
        showSymbol: false, symbolSize: 8, smooth: false, connectNulls: false,
        lineStyle: { width: 2, type: item.dashed ? 'dashed' : 'solid', cap: 'round', join: 'round' },
        itemStyle: bar
          ? { color: item.color, borderRadius: item.stack && index < options.series.length - 1 ? 0 : [4, 4, 0, 0], borderColor: tokens.surface, borderWidth: item.stack ? 1 : 0 }
          : { color: item.color, borderColor: tokens.surface, borderWidth: 2 },
        barMaxWidth: 24,
        areaStyle: item.area ? { color: item.color, opacity: item.stack ? 0.22 : 0.1 } : undefined,
        emphasis: { focus: 'series' },
        markLine: index === 0 && options.markLine ? {
          silent: true, symbol: 'none',
          lineStyle: { color: tokens.status.critical, width: 1, type: 'solid' },
          label: { color: tokens.textSecondary, fontSize: 11, formatter: options.markLine.label, position: 'insideEndTop' },
          data: [{ yAxis: options.markLine.value }],
        } : undefined,
      }
    }),
  }
}

export interface BarSeries { name: string; color: string; data: (number | null)[]; stack?: string }
export function barOption(tokens: ChartTokens, options: {
  categories: string[]
  series: BarSeries[]
  format: ValueFormatter
  horizontal?: boolean
  /** Direct value label at each bar tip (single-series bars only). */
  labels?: boolean
}): EChartsCoreOption {
  const showLegend = options.series.length > 1
  const category = {
    type: 'category', data: options.categories,
    axisLabel: { color: tokens.textSecondary, fontSize: 11, hideOverlap: !options.horizontal, width: options.horizontal ? 132 : undefined, overflow: 'truncate' },
    axisLine: { lineStyle: { color: tokens.axis } }, axisTick: { show: false },
  }
  const value = valueAxis(tokens, options.format)
  const last = options.series.length - 1
  return {
    color: options.series.map(item => item.color),
    legend: legend(tokens, showLegend),
    grid: { top: showLegend ? 36 : 8, right: options.labels && options.horizontal ? 56 : 12, bottom: 4, left: 4, containLabel: true },
    tooltip: {
      ...tooltipBase(tokens), trigger: 'axis', axisPointer: { type: 'shadow', shadowStyle: { color: tokens.dark ? 'rgba(255,255,255,0.04)' : 'rgba(31,38,51,0.04)' } },
      formatter: (items: any[]) => `<div style="color:${tokens.muted};margin-bottom:4px">${items[0]?.name ?? ''}</div>`
        + items.map(item => tooltipRow(tokens, item.color, item.seriesName, item.value == null ? '—' : options.format(Number(item.value)))).join(''),
    },
    xAxis: options.horizontal ? value : category,
    yAxis: options.horizontal ? { ...category, inverse: true } : value,
    series: options.series.map((item, index) => {
      const end = !item.stack || index === last
      const radius = end ? (options.horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0]) : 0
      return {
        name: item.name, type: 'bar', data: item.data, stack: item.stack, barMaxWidth: 24, barGap: '20%',
        itemStyle: { color: item.color, borderRadius: radius, borderColor: tokens.surface, borderWidth: item.stack ? 1 : 0 },
        label: options.labels && !item.stack ? {
          show: true, position: options.horizontal ? 'right' : 'top', color: tokens.textSecondary, fontSize: 11,
          formatter: (params: any) => options.format(Number(params.value)),
        } : undefined,
        emphasis: { focus: 'series' },
      }
    }),
  }
}

export function donutOption(tokens: ChartTokens, options: {
  data: { name: string; value: number; color: string }[]
  format: ValueFormatter
  title: string
  total?: string
}): EChartsCoreOption {
  const sum = options.data.reduce((total, item) => total + item.value, 0)
  return {
    color: options.data.map(item => item.color),
    tooltip: {
      ...tooltipBase(tokens), trigger: 'item',
      formatter: (item: any) => tooltipRow(tokens, item.color, item.name, `${options.format(item.value)} · ${sum ? (item.value / sum * 100).toFixed(1) : 0}%`),
    },
    legend: {
      orient: 'vertical', right: 0, top: 'middle', icon: 'roundRect', itemWidth: 10, itemHeight: 10, itemGap: 10,
      textStyle: { color: tokens.textSecondary, fontSize: 12 },
      formatter: (name: string) => {
        const item = options.data.find(row => row.name === name)
        return `${name}  ${item ? options.format(item.value) : ''}`
      },
    },
    title: {
      text: options.total ?? options.format(sum), subtext: options.title, left: '29%', top: '38%', textAlign: 'center',
      textStyle: { color: tokens.text, fontSize: 20, fontWeight: 600 },
      subtextStyle: { color: tokens.muted, fontSize: 11 },
    },
    series: [{
      type: 'pie', radius: ['58%', '78%'], center: ['30%', '50%'], avoidLabelOverlap: true,
      itemStyle: { borderColor: tokens.surface, borderWidth: 2, borderRadius: 4 },
      label: { show: false }, labelLine: { show: false },
      emphasis: { scale: true, scaleSize: 4 },
      data: sum ? options.data : [{ name: '暂无数据', value: 1, itemStyle: { color: tokens.grid }, tooltip: { show: false } }],
    }],
  }
}

/** Single-value meter; the fill colour carries severity, the track is a light step. */
export function gaugeOption(tokens: ChartTokens, options: {
  value: number | null
  max: number
  label: string
  format: ValueFormatter
  warn?: number
  critical?: number
}): EChartsCoreOption {
  const ratio = options.value == null || !options.max ? 0 : options.value / options.max
  const color = options.critical != null && ratio >= options.critical ? tokens.status.critical
    : options.warn != null && ratio >= options.warn ? tokens.status.serious : tokens.series[0]
  return {
    series: [{
      type: 'gauge', startAngle: 220, endAngle: -40, min: 0, max: options.max || 1, radius: '96%', center: ['50%', '58%'],
      progress: { show: true, width: 10, roundCap: true, itemStyle: { color } },
      axisLine: { roundCap: true, lineStyle: { width: 10, color: [[1, tokens.grid]] } },
      pointer: { show: false }, axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false },
      anchor: { show: false },
      title: { show: true, offsetCenter: [0, '34%'], color: tokens.muted, fontSize: 12 },
      detail: {
        valueAnimation: true, offsetCenter: [0, '-4%'], color: tokens.text, fontSize: 22, fontWeight: 600,
        formatter: () => options.value == null ? '—' : options.format(options.value),
      },
      data: [{ value: options.value ?? 0, name: options.label }],
    }],
  }
}

export function heatmapOption(tokens: ChartTokens, options: {
  xLabels: string[]
  yLabels: string[]
  data: [number, number, number][]
  format: ValueFormatter
}): EChartsCoreOption {
  const max = Math.max(1, ...options.data.map(item => item[2]))
  return {
    tooltip: {
      ...tooltipBase(tokens), trigger: 'item',
      formatter: (item: any) => `<div style="color:${tokens.muted};margin-bottom:4px">${options.yLabels[item.value[1]]} ${options.xLabels[item.value[0]]}:00</div>`
        + tooltipRow(tokens, item.color, '提交任务', options.format(item.value[2])),
    },
    grid: { top: 4, right: 8, bottom: 36, left: 4, containLabel: true },
    xAxis: {
      type: 'category', data: options.xLabels, splitArea: { show: false },
      axisLabel: { color: tokens.muted, fontSize: 10, interval: 2 }, axisLine: { show: false }, axisTick: { show: false },
    },
    yAxis: {
      type: 'category', data: options.yLabels, inverse: true,
      axisLabel: { color: tokens.textSecondary, fontSize: 11 }, axisLine: { show: false }, axisTick: { show: false },
    },
    visualMap: {
      min: 0, max, calculable: false, orient: 'horizontal', left: 'center', bottom: 0, itemWidth: 10, itemHeight: 120,
      inRange: { color: tokens.sequential }, textStyle: { color: tokens.muted, fontSize: 10 },
      formatter: (value: number) => options.format(value),
    },
    series: [{
      type: 'heatmap', data: options.data,
      itemStyle: { borderColor: tokens.surface, borderWidth: 2, borderRadius: 3 },
      emphasis: { itemStyle: { borderColor: tokens.text, borderWidth: 1 } },
    }],
  }
}

export function sparklineOption(options: { data: (number | null)[]; color: string }): EChartsCoreOption {
  return {
    grid: { top: 2, right: 2, bottom: 2, left: 2 },
    xAxis: { type: 'category', show: false, boundaryGap: false, data: options.data.map((_, index) => index) },
    yAxis: { type: 'value', show: false, min: (range: { min: number }) => Math.min(0, range.min) },
    tooltip: { show: false },
    series: [{
      type: 'line', data: options.data, showSymbol: false, smooth: false, silent: true,
      lineStyle: { width: 1.5, color: options.color }, areaStyle: { color: options.color, opacity: 0.1 },
    }],
  }
}
