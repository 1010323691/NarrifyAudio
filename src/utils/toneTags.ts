/**
 * 整章预览「常用语气标签建议」的纯文本变换（零 Vue 依赖）：
 *
 * chip 选中态与点击行为**全部以 instruct 文本为唯一真相**——
 *   - 选中态 = 该分类是否有标签词**整词**命中当前文本（手写 / 面板插入一视同仁）；
 *   - 点击标签 = 该词整词命中 → 移除；未命中 → 末尾追加（只增删自身，不动同类其他词）。
 * 因此用户手动增删文本会实时反映到 chip 高亮，手写进去的标签词同样被识别。
 *
 * 整词匹配（word-bounded）避免子串误判：``平静`` 不误命中 ``平静地``、``正常`` 不误命中 ``正常音量``。
 * 追加只在文本末尾按分隔符拼入，不触碰原文其余位置；移除只删该词及紧邻一个分隔符，不重排其余标点/空白。
 */

export interface ToneTagCategory {
  key: string
  label: string
  tags: string[]
}

export const TONE_TAG_CATEGORIES: ToneTagCategory[] = [
  { key: 'emotion', label: '情绪', tags: ['平静', '喜悦', '悲伤', '愤怒', '紧张', '恐惧', '惊讶', '失落'] },
  { key: 'attitude', label: '态度', tags: ['温柔', '严肃', '坚定', '犹豫', '冷淡', '讽刺', '无奈', '恳求'] },
  { key: 'expression', label: '表达', tags: ['疑问', '反问', '质问', '安慰', '命令', '警告', '试探'] },
  { key: 'rhythm', label: '节奏', tags: ['缓慢', '舒缓', '正常语速', '轻快', '急促', '停顿明显'] },
  { key: 'volume', label: '音量', tags: ['轻声', '低声', '耳语', '正常音量', '提高音量', '喊叫'] },
  { key: 'style', label: '风格', tags: ['自然叙述', '客观平稳', '娓娓道来', '悬疑铺陈', '庄重叙述'] },
]

const APPEND_SEP = '、' // 拼入分隔符，与占位符「如：平静地、压低声音」一致

export interface ConflictPair {
  a: string
  b: string
}

/** 常见「相反要求」词对（子串宽召回、非阻断、措辞留余地；纯数组，易扩展）。 */
export const TONE_CONFLICTS: ConflictPair[] = [
  { a: '耳语', b: '喊叫' },
  { a: '轻声', b: '喊叫' },
  { a: '低声', b: '喊叫' },
  { a: '缓慢', b: '急促' },
  { a: '舒缓', b: '急促' },
  { a: '平静', b: '愤怒' },
  { a: '坚定', b: '犹豫' },
]

/** 是否「词字符」（任一字形文字或数字）——CJK 无空格环境下的整词边界判定。 */
function isWordChar(ch: string): boolean {
  return !!ch && /[\p{L}\p{N}]/u.test(ch)
}

/** 找整词（前后均非词字符，或为串边界）出现；返回起始下标，未命中返回 -1。 */
function findWholeWord(text: string, word: string): number {
  if (!word) return -1
  for (let i = 0; i + word.length <= text.length; i++) {
    if (text.slice(i, i + word.length) !== word) continue
    const before = i > 0 ? text[i - 1] : ''
    const after = i + word.length < text.length ? text[i + word.length] : ''
    if (!isWordChar(before) && !isWordChar(after)) return i
  }
  return -1
}

const isSep = (ch: string): boolean => (ch ? /[\s、，,;；。]/.test(ch) : false)

/** 标签是否整词命中当前文本（手写 / 面板插入一视同仁）。 */
export function textHasTag(text: string, tag: string): boolean {
  return findWholeWord(text, tag) >= 0
}

/** 该分类命中的首个标签词（整词）；无命中返回 null。用于分类 chip 高亮。 */
export function selectedTagInCategory(text: string, category: string): string | null {
  const cat = TONE_TAG_CATEGORIES.find((c) => c.key === category)
  if (!cat) return null
  for (const tag of cat.tags) if (findWholeWord(text, tag) >= 0) return tag
  return null
}

/** 追加：末尾按分隔符拼入；文本（trim 后）为空则直接返回标签。只改末尾，不动原文。 */
export function appendTag(text: string, tag: string): string {
  if (!text.trim()) return tag
  const lastChar = text[text.length - 1]
  const needsSep = !/[\s、，,;；。]/.test(lastChar)
  return text + (needsSep ? APPEND_SEP : '') + tag
}

/** 移除一次整词出现：顺带吃掉一个紧邻分隔符；若开头变分隔符则去掉那一个。不重排其余标点/空白。 */
function removeTag(text: string, tag: string): string {
  if (!tag) return text
  const idx = findWholeWord(text, tag)
  if (idx < 0) return text
  const afterIdx = idx + tag.length
  const before = idx > 0 ? text[idx - 1] : ''
  const after = afterIdx < text.length ? text[afterIdx] : ''
  let start = idx
  let end = afterIdx
  if (before && isSep(before)) start = idx - 1
  else if (after && isSep(after)) end = afterIdx + 1
  let result = text.slice(0, start) + text.slice(end)
  if (isSep(result[0])) result = result.slice(1)
  return result
}

/** 点击标签：整词命中 → 移除（toggle off）；未命中 → 末尾追加。只增删自身，不碰同类其他词（含手写）。 */
export function toggleTag(text: string, tag: string): string {
  return textHasTag(text, tag) ? removeTag(text, tag) : appendTag(text, tag)
}

/** 冲突检测（全文、非阻断、来源无关）：返回两词**同时出现**的对（子串宽召回）。 */
export function findToneConflicts(text: string): ConflictPair[] {
  if (!text) return []
  return TONE_CONFLICTS.filter((p) => text.includes(p.a) && text.includes(p.b))
}
