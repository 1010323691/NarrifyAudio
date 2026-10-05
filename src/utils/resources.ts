import type { ResourceEntry, ResourceProject } from '@/api/resources'
import type { TaskSnapshot } from '@/types'
import { formatBytes } from '@/utils/format'

export const RESOURCE_FILTERS = [
  { key: 'deliverables', label: '可下载成品', module: '' },
  { key: 'production', label: '全部制作资料', module: '' },
  { key: 'all', label: '全部业务文件', module: '' },
  { key: 'text', label: '文本与解析', module: '' },
  { key: '01_input', label: '原始文件', module: '01_input' },
  { key: '02_split_text', label: '章节文本', module: '02_split_text' },
  { key: '03_parsed_json', label: '解析结果', module: '03_parsed_json' },
  { key: '04_voice_profiles', label: '角色资料', module: '04_voice_profiles' },
  { key: 'audio', label: '制作音频', module: '' },
  { key: '05_audio_chunk', label: '合成片段', module: '05_audio_chunk', nested: true },
  { key: '06_audio_merge', label: '合并音频', module: '06_audio_merge', nested: true },
  { key: '07_output', label: '最终成品', module: '07_output', nested: true },
  { key: '08_bgm', label: 'BGM资料与混音', module: '08_bgm' },
  { key: 'other', label: '其他文件', module: 'other' },
  { key: 'system', label: '系统文件', module: '' },
] as const
export const RESOURCE_STAGES: Record<string, string> = {
  '01_input': '/text', '02_split_text': '/text', '03_parsed_json': '/script',
  '04_voice_profiles': '/voices', '05_audio_chunk': '/batch', '06_audio_merge': '/merge',
  '07_output': '/audio', '08_bgm': '/bgm',
}
export function resourceBytes(value: number | null | undefined): string {
  return value == null ? '未读取' : formatBytes(value, { emptyText: '0 B', lowRange: 'clamp', decimals: 'always-one', nonFiniteText: '未读取' })
}
export function resourceDate(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString([], { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}
export function categoryCount(project: ResourceProject, ...keys: string[]): number {
  return project.snapshot?.categories.filter(item => keys.includes(item.key)).reduce((sum, item) => sum + item.count, 0) ?? 0
}
export function businessCount(project: ResourceProject): number {
  return project.snapshot?.categories.filter(item => !['config', 'logs', '00_temp', '.cache', 'cache'].includes(item.key)).reduce((sum, item) => sum + item.count, 0) ?? 0
}
export function filterCount(project: ResourceProject | null, key: string): number | null {
  if (!project?.snapshot) return null
  if (key === 'deliverables') return project.delivery_count
  if (key === 'production') return Math.max(0, businessCount(project) - project.delivery_count)
  if (key === 'all') return businessCount(project)
  if (key === 'audio') return project.snapshot.audio_count
  if (key === 'text') return categoryCount(project, '01_input', '02_split_text', '03_parsed_json')
  if (key === 'system') return categoryCount(project, 'config', 'logs')
  return categoryCount(project, key)
}

export function materialCount(project: ResourceProject | null, key: string): number | null {
  if (!project?.snapshot) return null
  if (key === 'audio') return project.production_audio_count
  const categories = project.production_categories || []
  const keys = key === 'production' ? categories.map(item => item.key) : key === 'text' ? ['01_input', '02_split_text', '03_parsed_json'] : [key]
  return categories.filter(item => keys.includes(item.key)).reduce((sum, item) => sum + item.count, 0)
}
export function isActiveResourceTask(task: TaskSnapshot): boolean {
  return ['pending', 'queued', 'retrying', 'running', 'paused', 'cancelling'].includes(task.status)
}
export function taskProgressLabel(task: TaskSnapshot): string {
  if (task.status === 'paused') return '已暂停'
  if (['pending', 'queued', 'retrying'].includes(task.status)) return '等待执行'
  if (task.status === 'cancelling') return '取消中'
  if (task.seg_total && task.seg_total > 0) return `${task.seg_done ?? 0}/${task.seg_total}段`
  if (task.progress > 0) return `${Math.round(Math.min(1, task.progress) * 100)}%`
  return task.phase || '处理中'
}
export function entryIsSystem(entry: ResourceEntry): boolean {
  return ['config', 'logs'].includes(entry.module)
}
