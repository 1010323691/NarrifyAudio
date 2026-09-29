/** Display labels for the text-format workbench. Semantic matter text comes
 * from the backend (matters[].text); these are the fixed vocabulary bits the
 * server does not carry: stage names, split modes, review states. */

export const STAGE_LABELS: Record<string, string> = {
  format: '排版',
  analyze: '章节分析',
  split: '分册',
}

export function stageLabel(stage: string | null | undefined): string {
  return stage ? (STAGE_LABELS[stage] ?? stage) : '流程'
}

export const MODE_LABELS: Record<string, string> = {
  smart: '智能分册',
  by_length: '按字数分册',
  whole_book: '整本处理',
}

export function modeLabel(mode: string | null | undefined): string {
  return mode ? (MODE_LABELS[mode] ?? mode) : '未分册'
}

/** reason 代码 → 中文标签：处理说明里的原因列表用；未收录的代码原样展示。 */
export const REASON_LABELS: Record<string, string> = {
  duplicate_number: '编号重复',
  duplicate_split: '正文重复拆分',
  duplicate_kept: '重复章节保留',
  truncated: '截除重复正文',
  renumbered: '编号规范化',
  gap_absorbed: '跳号合并',
  range: '范围标题补齐',
  range_mid: '段落切点补齐',
  inferred: '推断拆分',
  mechanical: '机械拆分',
  length_split: '按字数分册',
  whole_book: '整本处理',
  kept: '保留原样',
}

/** 章节表「核对原因」列的简要标签：比 matter 长文案更短，advisory 原因优先。 */
export const REASON_BRIEF: Record<string, string> = {
  duplicate_number: '原章节号重复',
  duplicate_kept: '原章节号重复',
  duplicate_split: '重复正文拆分',
  truncated: '重复正文已截除',
  inferred: '推断章节边界',
  mechanical: '按结构线索拆分',
  range_mid: '切点段落对齐',
  range: '范围标题补齐',
  renumbered: '编号已规范化',
  gap_absorbed: '跳号已并入',
  length_split: '按字数分册',
  whole_book: '整本处理',
}

const ADVISORY_REASONS = ['duplicate_number', 'duplicate_kept', 'duplicate_split', 'truncated', 'inferred', 'mechanical', 'range_mid']

/** 由 reasons 派生表格用简要原因：advisory 优先；编号重复类附出现次数；无调整 → — */
export function chapterBriefLabel(reasons: string[], dupCount = 0): string {
  const adv = reasons.find(r => ADVISORY_REASONS.includes(r))
  const code = adv ?? reasons.find(r => r !== 'kept' && REASON_BRIEF[r]) ?? ''
  if (!code) return '—'
  const base = REASON_BRIEF[code] ?? code
  return (code === 'duplicate_number' || (code === 'duplicate_kept' && dupCount >= 2)) && dupCount >= 2
    ? `${base} ${dupCount} 次`
    : base
}

/** 章节号零补齐：位数以最大章节号位数为标准（如最多 339 章 → 第001章）。 */
export function padChapterNum(value: number | string | null | undefined, width: number): string {
  const s = value == null || value === '' ? '' : String(value)
  return s.length >= width ? s : s.padStart(width, '0')
}

/** 由章节列表推导编号位数（取最大章节号的位数，至少 1 位）。 */
export function chapterNumWidth(chapters: Array<{ num: number | null; seq: number }>): number {
  const maxNum = chapters.reduce((m, c) => Math.max(m, c.num ?? c.seq), 1)
  return String(maxNum).length
}

export function reasonLabel(code: string): string {
  return REASON_LABELS[code] ?? code
}

export function confidenceLabel(c: string): string {
  return c === 'high' ? '高' : c === 'medium' ? '中' : c === 'low' ? '低' : c
}

export function confidenceClass(c: string): string {
  return c === 'high'
    ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300'
    : c === 'medium'
      ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
      : 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300'
}
