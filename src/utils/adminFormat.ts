// Formatting shared by the admin console views and chart tooltips.
import { formatBytes, type BytesFormat } from '@/utils/format'

// 管理端大小口径：缺失显示「未采集」，<1024 原样（含负值），KB+ 一律 1 位小数
const ADMIN_BYTES: BytesFormat = { emptyText: '未采集', lowRange: 'raw', decimals: 'always-one' }
export function bytes(value?: number | null): string {
  return formatBytes(value, ADMIN_BYTES)
}

const compactFormatter = new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 })
const plainFormatter = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 })
/** 1,284 / 1.3万 / 2.4亿 — big standalone numbers; null → —. */
export function compact(value?: number | null): string {
  if (value == null || Number.isNaN(value)) return '—'
  return Math.abs(value) < 10000 ? plainFormatter.format(value) : compactFormatter.format(value)
}
export function plain(value?: number | null, digits = 1): string {
  if (value == null || Number.isNaN(value)) return '—'
  return value.toLocaleString('zh-CN', { maximumFractionDigits: digits })
}
export function percent(value?: number | null, digits = 1): string {
  return value == null || Number.isNaN(value) ? '—' : `${value.toFixed(digits)}%`
}

export function date(value?: string | null): string {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
}
export function shortTime(value?: string | null): string {
  return value ? new Date(value).toLocaleTimeString('zh-CN', { hour12: false }) : '—'
}
/** "3 分钟前" style relative time for recent activity. */
export function relative(value?: string | null): string {
  if (!value) return '—'
  const seconds = Math.round((Date.now() - new Date(value).getTime()) / 1000)
  if (seconds < 0) return date(value)
  if (seconds < 60) return `${seconds} 秒前`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`
  return `${Math.floor(seconds / 86400)} 天前`
}

export function seconds(value?: number | null): string {
  if (value == null) return '—'
  const total = Math.max(0, value)
  if (total < 1) return `${Math.round(total * 1000)} ms`
  if (total < 60) return `${total.toFixed(total < 10 ? 1 : 0)} 秒`
  if (total < 3600) return `${Math.floor(total / 60)} 分 ${Math.round(total % 60)} 秒`
  return `${Math.floor(total / 3600)} 小时 ${Math.floor(total % 3600 / 60)} 分`
}
export function elapsed(start?: string | null, end?: string | null): string {
  if (!start) return '—'
  return seconds(((end ? new Date(end).getTime() : Date.now()) - new Date(start).getTime()) / 1000)
}

export type Tone = 'positive' | 'warning' | 'negative' | 'neutral'
export function tone(status: string): Tone {
  if (['healthy', 'succeeded', 'completed', 'idle'].includes(status)) return 'positive'
  if (['warning', 'queued', 'pending', 'retrying', 'processing', 'running', 'cancelling', 'starting', 'busy'].includes(status)) return 'warning'
  if (['error', 'failed', 'timeout', 'offline'].includes(status)) return 'negative'
  return 'neutral'
}
const STATUS_LABELS: Record<string, string> = {
  healthy: '正常', warning: '警告', error: '异常', unknown: '未采集', pending: '排队中', queued: '排队中',
  cancelling: '取消中', running: '运行中', processing: '运行中', retrying: '重试中', succeeded: '已完成', completed: '已完成',
  failed: '失败', timeout: '超时', cancelled: '已取消', idle: '空闲', offline: '离线', starting: '启动中', busy: '忙碌', paused: '已暂停',
}
export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status
}
export function metricCount(statuses: Record<string, number> | undefined, ...keys: string[]): number {
  return keys.reduce((total, key) => total + (statuses?.[key] ?? 0), 0)
}
/** Signed change vs the previous period: { text: '+12%', direction }. */
export function delta(current: number, previous: number): { text: string; direction: 'up' | 'down' | 'flat' } | null {
  if (!previous && !current) return null
  if (!previous) return { text: '新增', direction: 'up' }
  const change = (current - previous) / previous * 100
  if (Math.abs(change) < 0.5) return { text: '持平', direction: 'flat' }
  return { text: `${change > 0 ? '+' : ''}${change.toFixed(Math.abs(change) < 10 ? 1 : 0)}%`, direction: change > 0 ? 'up' : 'down' }
}
