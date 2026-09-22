// Display helpers — mirror the backend's formatting so the UI and backend agree.

/** 90 -> "1:30", 3723 -> "1:02:03" (mirrors engines/audio.format_duration). */
export function formatDuration(totalSeconds: number | null | undefined): string {
  if (totalSeconds == null || Number.isNaN(totalSeconds)) return '—'
  const s = Math.max(0, Math.round(totalSeconds))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
  return `${m}:${String(sec).padStart(2, '0')}`
}

/** 1536 -> "1.5 KB", 1048576 -> "1.0 MB" (mirrors engines/audio.format_bytes). */
export function formatBytes(b: number | null | undefined): string {
  if (b == null || Number.isNaN(b)) return '—'
  if (b < 1024) return `${b} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let n = b
  let i = -1
  for (;;) {
    n /= 1024
    i++
    if (!(n >= 1024 && i < units.length - 1)) break
  }
  const digits = n >= 100 || i === 0 ? 0 : 1
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
