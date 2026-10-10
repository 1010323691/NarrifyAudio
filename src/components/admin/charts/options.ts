// Spec builders: plain data describing a chart. AdminChart dispatches a spec to
// the matching SVG component, so views stay free of drawing details.
import type { ChartTokens } from './tokens'
import type { TimeScale } from './scale'

export type { TimeScale }
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
export interface BarSeries { name: string; color: string; data: (number | null)[]; stack?: string }
export interface DonutDatum { name: string; value: number; color: string }

export interface TimeSpec {
  kind: 'time'
  series: TimeSeries[]
  format: ValueFormatter
  scale: TimeScale
  max?: number
  decimals?: boolean
  fit?: boolean
  markLine?: { value: number; label: string }
}
export interface BarSpec {
  kind: 'bar'
  categories: string[]
  series: BarSeries[]
  format: ValueFormatter
  horizontal?: boolean
  labels?: boolean
}
export interface DonutSpec { kind: 'donut'; data: DonutDatum[]; format: ValueFormatter; title: string; total?: string }
export interface GaugeSpec {
  kind: 'gauge'
  value: number | null
  max: number
  label: string
  format: ValueFormatter
  warn?: number
  critical?: number
}
export interface HeatmapSpec {
  kind: 'heatmap'
  xLabels: string[]
  yLabels: string[]
  data: [number, number, number][]
  format: ValueFormatter
}
export interface SparkSpec { kind: 'spark'; data: (number | null)[]; color: string }
export type ChartSpec = TimeSpec | BarSpec | DonutSpec | GaugeSpec | HeatmapSpec | SparkSpec

export function timeSeriesOption(_tokens: ChartTokens, options: Omit<TimeSpec, 'kind'>): TimeSpec {
  return { kind: 'time', ...options }
}
export function barOption(_tokens: ChartTokens, options: Omit<BarSpec, 'kind'>): BarSpec {
  return { kind: 'bar', ...options }
}
export function donutOption(_tokens: ChartTokens, options: Omit<DonutSpec, 'kind'>): DonutSpec {
  return { kind: 'donut', ...options }
}
/** Single-value meter; the fill colour carries severity, the track is a light step. */
export function gaugeOption(_tokens: ChartTokens, options: Omit<GaugeSpec, 'kind'>): GaugeSpec {
  return { kind: 'gauge', ...options }
}
export function heatmapOption(_tokens: ChartTokens, options: Omit<HeatmapSpec, 'kind'>): HeatmapSpec {
  return { kind: 'heatmap', ...options }
}
export function sparklineOption(options: Omit<SparkSpec, 'kind'>): SparkSpec {
  return { kind: 'spark', ...options }
}
