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
