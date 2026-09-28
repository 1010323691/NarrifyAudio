/**
 * 整章预览行状态机（纯函数，零 store / 零 api 依赖 —— scripts/test-state-isolation.mjs 回归钉住）：
 * previewReady 的唯一口径 = staged 内容 == 页内草稿。绝不与磁盘 03 比——磁盘 03 正是修改前
 * 的旧值，正常修改流程下 staged 与磁盘永不一致（按「与磁盘比」永远无法进入可保存状态）。
 * 服务端 /apply 的逐句门禁是最终裁决（同一口径：staged.ok + 产物存在 + 三元组全等），
 * 本文件只做前端提前禁用与状态呈现。
 */
export interface LineTriple {
  text: string
  speaker: string
  instruct: string
}

/** state.json 行（GET /api/tts/preview/chapter 响应中的 ``staged`` 字段）。 */
export interface StagedLine {
  ok: boolean
  reason: string
  text: string
  speaker: string
  instruct: string
  rendered_at: string
  fingerprint: string
  /** worker 报告的 [segment] ok 实际产物文件名（.mp3 或 .wav 回退）——试听 URL 与门禁以它为准。 */
  file: string
}

export type PreviewLineStatus = 'normal' | 'dirty' | 'rendering' | 'previewReady' | 'failed'

/** 后端口径：build_segments / 有效三元组比较均 strip。 */
const strip = (value: string): string => (value || '').trim()

export function triplesEqual(a: LineTriple, b: LineTriple): boolean {
  return (
    strip(a.text) === strip(b.text) &&
    strip(a.speaker) === strip(b.speaker) &&
    strip(a.instruct) === strip(b.instruct)
  )
}

/** 草稿与磁盘行任一字段不同 → dirty（待重渲染，禁止参与保存）。 */
export function isDirty(draft: LineTriple, disk: LineTriple): boolean {
  return !triplesEqual(draft, disk)
}

/** 前端推导 previewReady：重渲染产物与当前草稿完全一致 → 该句可参与保存。 */
export function isPreviewReady(staged: StagedLine | null | undefined, draft: LineTriple): boolean {
  return !!staged && staged.ok === true && triplesEqual(staged, draft)
}

/**
 * F5 / 重开恢复：staged.ok 且内容 ≠ 磁盘行 → 以 staged 内容作为草稿（重开页面后该句直接
 * previewReady，已渲染的编辑不丢，无需重渲染即可保存）；否则草稿 = 磁盘行。
 */
export function restoreDraft(staged: StagedLine | null | undefined, disk: LineTriple): LineTriple {
  if (staged && staged.ok === true && !triplesEqual(staged, disk)) {
    return { text: staged.text, speaker: staged.speaker, instruct: staged.instruct }
  }
  return { text: disk.text, speaker: disk.speaker, instruct: disk.instruct }
}

/**
 * 行状态（优先级）：rendering（重渲染在途）> failed（重渲染失败：任务失败或 staged.ok=false）
 * > previewReady > dirty > normal。staged.ok=true 但与草稿不一致 = 旧产物，不算 failed，
 * 按 dirty 处理（用户又改了内容，需要重新渲染）。
 */
export function lineStatus(args: {
  disk: LineTriple
  draft: LineTriple
  staged: StagedLine | null | undefined
  rendering: boolean
  renderFailed: boolean
}): PreviewLineStatus {
  const { disk, draft, staged, rendering, renderFailed } = args
  if (rendering) return 'rendering'
  if (renderFailed || (staged !== null && staged !== undefined && !staged.ok)) return 'failed'
  if (isPreviewReady(staged, draft)) return 'previewReady'
  if (isDirty(draft, disk)) return 'dirty'
  return 'normal'
}

/** 保存门禁（服务端 /apply 同口径的前置判断）：存在 dirty 句，且全部 dirty 句 previewReady、
 *  本章无在途重渲染 → 可保存；``pending`` = 未完成重渲染的修改句数（按钮禁用文案用）。 */
export function saveState(
  rows: { disk: LineTriple; draft: LineTriple; staged: StagedLine | null; rendering: boolean }[],
): { ready: boolean; dirtyCount: number; pending: number } {
  let dirtyCount = 0
  let pending = 0
  for (const row of rows) {
    if (!isDirty(row.draft, row.disk)) continue
    dirtyCount += 1
    if (row.rendering || !isPreviewReady(row.staged, row.draft)) pending += 1
  }
  return { ready: dirtyCount > 0 && pending === 0, dirtyCount, pending }
}

/**
 * 由草稿 vs 磁盘的差集构建 edits 数组（partial triple：只携带被改字段，strip 口径与服务端
 * 有效三元组一致）。全同的句不产生 edit。
 */
export function buildEdits(
  rows: { index: number; disk: LineTriple; draft: LineTriple }[],
): { index: number; text?: string; speaker?: string; instruct?: string }[] {
  const edits: { index: number; text?: string; speaker?: string; instruct?: string }[] = []
  for (const row of rows) {
    const edit: { index: number; text?: string; speaker?: string; instruct?: string } = { index: row.index }
    if (strip(row.draft.text) !== strip(row.disk.text)) edit.text = strip(row.draft.text)
    if (strip(row.draft.speaker) !== strip(row.disk.speaker)) edit.speaker = strip(row.draft.speaker)
    if (strip(row.draft.instruct) !== strip(row.disk.instruct)) edit.instruct = strip(row.draft.instruct)
    if (edit.text !== undefined || edit.speaker !== undefined || edit.instruct !== undefined) edits.push(edit)
  }
  return edits
}
