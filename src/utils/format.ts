// Display helpers — mirror the backend's formatting so the UI and backend agree.

/** 90 -> "1:30", 3723 -> "1:02:03" (mm:ss / h:mm:ss). */
export function formatDuration(totalSeconds: number | null | undefined): string {
  if (totalSeconds == null || Number.isNaN(totalSeconds)) return '—'
  const s = Math.max(0, Math.round(totalSeconds))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
  return `${m}:${String(sec).padStart(2, '0')}`
}

/** 每个调用点的大小文案语义（S8/Q11）：
 * emptyText — null/NaN 显示；lowRange — <1024 行为（'raw' 原样含负值，'clamp' 非正一律 0 B、B 档取整）；
 * decimals — KB+ 档小数规则（'smart' 即 KB 档或 ≥100 用 0 位、其余 1 位；'always-one' 一律 1 位）；
 * nonFiniteText — 可选，±Infinity 的显示（NaN 仍走 emptyText）。 */
export interface BytesFormat {
  emptyText: string
  lowRange: 'raw' | 'clamp'
  decimals: 'smart' | 'always-one'
  nonFiniteText?: string
}

const DEFAULT_BYTES_FORMAT: BytesFormat = { emptyText: '—', lowRange: 'raw', decimals: 'smart' }

/** 1536 -> "1.5 KB", 1048576 -> "1.0 MB" (B / KB / MB …). */
export function formatBytes(b: number | null | undefined, format: BytesFormat = DEFAULT_BYTES_FORMAT): string {
  if (b == null || Number.isNaN(b)) return format.emptyText
  if (format.nonFiniteText !== undefined && !Number.isFinite(b)) return format.nonFiniteText
  if (format.lowRange === 'clamp' && b <= 0) return '0 B'
  if (b < 1024) {
    if (format.lowRange === 'raw') return `${b} B`
    // B 档取整；(0,1) 分数字节保留旧副本的负指数档显示
    return b >= 1 ? `${b.toFixed(0)} B` : `${(b * 1024).toFixed(1)} B`
  }
  const units = ['KB', 'MB', 'GB', 'TB']
  let n = b
  let i = -1
  for (;;) {
    n /= 1024
    i++
    if (!(n >= 1024 && i < units.length - 1)) break
  }
  const digits = format.decimals === 'always-one' ? 1 : (n >= 100 || i === 0 ? 0 : 1)
  return `${n.toFixed(digits)} ${units[i]}`
}

/** 1234567 -> "1,234,567" (mirrors engines/book.format_number). */
export function formatNumber(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return '–'
  const neg = n < 0
  let s = String(Math.trunc(Math.abs(n)))
  const parts: string[] = []
  while (s.length > 3) {
    parts.unshift(s.slice(-3))
    s = s.slice(0, -3)
  }
  parts.unshift(s)
  return (neg ? '-' : '') + parts.join(',')
}

/** Format an epoch-seconds timestamp as a local HH:MM:SS clock string. */
export function formatClock(ts: number): string {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour12: false })
}
