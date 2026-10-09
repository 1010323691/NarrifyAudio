// Point-list helpers shared by the admin views' charts.
import type { MetricPoint } from '@/api/admin'

type Point = { time: string } & Record<string, unknown>

/** [[time, value]] for a chart series; missing samples stay null so lines break instead of lying. */
export function series(points: Point[] | undefined, key: string, scale = 1): [string, number | null][] {
  return (points ?? []).map(point => {
    const value = point[key]
    return [point.time, typeof value === 'number' ? value * scale : null]
  })
}

/** Sum of several keys per point (null when none of them were sampled). */
export function summed(points: Point[] | undefined, keys: string[]): [string, number | null][] {
  return (points ?? []).map(point => {
    const values = keys.map(key => point[key]).filter((value): value is number => typeof value === 'number')
    return [point.time, values.length ? values.reduce((a, b) => a + b, 0) : null]
  })
}

export function values(points: Point[] | undefined, key: string): (number | null)[] {
  return series(points, key).map(([, value]) => value)
}

export function hasKey(points: MetricPoint[] | undefined, key: string): boolean {
  return !!points?.some(point => typeof point[key] === 'number')
}

export function gpuIndexes(points: MetricPoint[] | undefined): number[] {
  const found = new Set<number>()
  for (const point of points ?? []) for (const key of Object.keys(point)) {
    const match = /^gpu(\d+)_util$/.exec(key)
    if (match) found.add(Number(match[1]))
  }
  return [...found].sort((a, b) => a - b)
}

/** Time-scale of the x axis labels for a bucket step. */
export function scaleFor(stepSeconds: number, spanSeconds: number): 'minute' | 'hour' | 'day' {
  if (stepSeconds >= 86400) return 'day'
  return spanSeconds <= 86400 ? 'minute' : 'hour'
}

/** Axis labels for the throughput ranges: hours for a day, date+hour for a week, dates beyond. */
export function throughputScale(range: string | undefined): 'minute' | 'hour' | 'day' {
  return range === '30d' ? 'day' : range === '7d' ? 'hour' : 'minute'
}
