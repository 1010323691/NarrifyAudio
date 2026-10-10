// Pure geometry/format helpers for the SVG charts (no DOM, unit-testable).
export type TimeScale = 'minute' | 'hour' | 'day'

const pad = (value: number) => String(value).padStart(2, '0')

export function timeLabel(value: number, scale: TimeScale): string {
  const time = new Date(value)
  if (scale === 'minute') return `${pad(time.getHours())}:${pad(time.getMinutes())}`
  if (scale === 'hour') return `${pad(time.getMonth() + 1)}-${pad(time.getDate())} ${pad(time.getHours())}:00`
  return `${pad(time.getMonth() + 1)}-${pad(time.getDate())}`
}

export function tooltipTime(value: number, scale: TimeScale): string {
  const time = new Date(value)
  const day = `${pad(time.getMonth() + 1)}-${pad(time.getDate())}`
  return scale === 'day' ? day : `${day} ${pad(time.getHours())}:${pad(time.getMinutes())}`
}

/** 1 / 2 / 2.5 / 5 × 10ⁿ step at or above `raw`. */
export function niceStep(raw: number): number {
  if (!(raw > 0) || !Number.isFinite(raw)) return 1
  const power = 10 ** Math.floor(Math.log10(raw))
  const fraction = raw / power
  return (fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10) * power
}

export interface Ticks { ticks: number[]; min: number; max: number }

/** Axis ticks over [lo, hi]. `fixedMax` keeps `hi` as the domain top instead of rounding it up. */
export function niceTicks(lo: number, hi: number, options: { count?: number; integer?: boolean; fixedMax?: boolean } = {}): Ticks {
  const count = options.count ?? 4
  if (!(hi > lo)) hi = lo + (options.integer ? 1 : Math.abs(lo) || 1)
  let step = niceStep((hi - lo) / count)
  if (options.integer) step = Math.max(1, Math.ceil(step))
  const min = Math.floor(lo / step) * step
  const max = options.fixedMax ? hi : Math.ceil(hi / step) * step
  const ticks: number[] = []
  for (let value = min; value <= max + step * 1e-6; value += step) ticks.push(Number(value.toFixed(10)))
  return { ticks, min, max }
}

/** Rough text width — CJK glyphs are full width, everything else ~0.56em. */
export function textWidth(text: string, size = 11): number {
  let width = 0
  for (const char of text) width += /[⺀-鿿＀-￯]/.test(char) ? size : size * 0.56
  return width
}

export function truncate(text: string, maxWidth: number, size = 11): string {
  if (textWidth(text, size) <= maxWidth) return text
  let out = ''
  for (const char of text) {
    if (textWidth(out + char + '…', size) > maxWidth) break
    out += char
  }
  return `${out}…`
}

const fixed = (value: number) => Number(value.toFixed(2))

/** Polyline path segments; a null value breaks the line instead of interpolating. */
export function segments<T extends { x: number; y: number | null }>(points: T[]): (T & { y: number })[][] {
  const out: (T & { y: number })[][] = []
  let current: (T & { y: number })[] = []
  for (const point of points) {
    if (point.y == null) {
      if (current.length) out.push(current)
      current = []
    } else current.push(point as T & { y: number })
  }
  if (current.length) out.push(current)
  return out
}

export function linePath(segment: { x: number; y: number }[]): string {
  // A lone sample becomes a zero-length stroke so round caps still paint a dot.
  if (segment.length === 1) return `M${fixed(segment[0].x)},${fixed(segment[0].y)}h0`
  return segment.map((point, index) => `${index ? 'L' : 'M'}${fixed(point.x)},${fixed(point.y)}`).join('')
}

/** Closed area between a segment and a (possibly per-point) baseline. */
export function areaPath(segment: { x: number; y: number }[], base: number[]): string {
  const top = linePath(segment)
  const back = segment.map((point, index) => `L${fixed(point.x)},${fixed(base[index])}`).reverse().join('')
  return `${top}${back}Z`
}

/** Rect path with independent corner radii (used to round only the data end of a bar). */
export function roundedRect(x: number, y: number, w: number, h: number, r: { tl?: number; tr?: number; br?: number; bl?: number }): string {
  const limit = Math.min(w, h) / 2
  const tl = Math.min(r.tl ?? 0, limit), tr = Math.min(r.tr ?? 0, limit)
  const br = Math.min(r.br ?? 0, limit), bl = Math.min(r.bl ?? 0, limit)
  return `M${fixed(x + tl)},${fixed(y)}H${fixed(x + w - tr)}${tr ? `A${tr},${tr} 0 0 1 ${fixed(x + w)},${fixed(y + tr)}` : ''}`
    + `V${fixed(y + h - br)}${br ? `A${br},${br} 0 0 1 ${fixed(x + w - br)},${fixed(y + h)}` : ''}`
    + `H${fixed(x + bl)}${bl ? `A${bl},${bl} 0 0 1 ${fixed(x)},${fixed(y + h - bl)}` : ''}`
    + `V${fixed(y + tl)}${tl ? `A${tl},${tl} 0 0 1 ${fixed(x + tl)},${fixed(y)}` : ''}Z`
}

/** Index of the entry in a sorted list closest to `value`. */
export function nearestIndex(sorted: number[], value: number): number {
  let best = 0
  for (let index = 1; index < sorted.length; index++) {
    if (Math.abs(sorted[index] - value) < Math.abs(sorted[best] - value)) best = index
  }
  return best
}

/** Sparkline paths in the 100×32 logical box of the design doc (y∈[4,30], zero baseline). */
export function sparkPaths(values: (number | null)[], max?: number): { line: string; area: string } {
  if (values.length < 2) return { line: '', area: '' }
  const top = Math.max(max || 0, ...values.map(value => value ?? 0), 1e-9)
  const points = values.map((value, index) => ({
    x: (index / (values.length - 1)) * 100,
    y: value == null ? null : 30 - (value / top) * 26,
  }))
  const parts = segments(points)
  return {
    line: parts.map(linePath).join(''),
    area: parts.map(part => areaPath(part, part.map(() => 32))).join(''),
  }
}

/** Gauge arc: 270° sweep on r=50 (circumference 314.2, visible arc 235.6). */
export const GAUGE_CIRC = 2 * Math.PI * 50
export const GAUGE_ARC = GAUGE_CIRC * 0.75
export function gaugeDash(ratio: number): string {
  const clamped = Math.max(0, Math.min(1, ratio))
  return `${(GAUGE_ARC * clamped).toFixed(1)} ${GAUGE_CIRC.toFixed(1)}`
}

/** Donut slices: dasharray length + offset per part, clockwise from 12 o'clock. */
export function donutSlices(values: number[], circumference: number, gap = 0): { length: number; offset: number }[] {
  const total = values.reduce((sum, value) => sum + value, 0)
  if (!(total > 0)) return values.map(() => ({ length: 0, offset: 0 }))
  const visible = values.filter(value => value > 0).length
  const useGap = visible > 1 ? gap : 0
  let offset = 0
  return values.map(value => {
    const full = (value / total) * circumference
    const slice = { length: Math.max(0, full - (value > 0 ? useGap : 0)), offset }
    offset += full
    return slice
  })
}
