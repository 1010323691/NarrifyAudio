/**
 * 角色名 → 稳定 HSL 色（FNV-1a 哈希取色相）。
 * 语义边界：仅保证「同名角色同色」——用于同行跨行/跨章节的同名角色辨识；角色改名后颜色
 * 可能变化（v1 接受；如需改名前后同色，后续引入稳定 speaker_id 再做）。
 */
export function speakerHue(speaker: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < speaker.length; i++) {
    h ^= speaker.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return (h >>> 0) % 360
}

/** 徽章配色：text = 角色名色，background = 同色相低透明度（深浅主题通用）。 */
export function speakerColor(speaker: string): { color: string; background: string } {
  const hue = speakerHue((speaker || '').trim())
  return {
    color: `hsl(${hue} 60% 38%)`,
    background: `hsl(${hue} 70% 50% / 0.16)`,
  }
}

/** 章节内角色集 → 色相表（章节预览详情面板）：按给定顺序（条目首次出现序）均分色环，
 *  相邻角色色差 = 360/N——N 个角色能达到的最大互斥间隔，「不同角色不同色且尽量不相近」
 *  （不再用随机哈希取相——哈希对会撞出相近色相）。
 *  NARRATOR 不参与色环分配（叙述者不是角色，消费方统一中性灰）。
 *  色相起点 210°（蓝紫区）。语义边界：映射依赖章节角色集，角色集变化时颜色随之变化
 *  （v1 接受，与原 speakerColor 语义一致）。
 *  原 speakerColor / speakerHue 哈希档为 ChapterPreview.vue:761 保留（:style 整对象绑定，返回形状不可变）。 */
export function distinctSpeakerHues(speakers: string[]): Record<string, number> {
  const chars = speakers.filter((s) => s !== 'NARRATOR')
  const n = chars.length
  const map: Record<string, number> = { NARRATOR: 0 }
  chars.forEach((s, i) => {
    map[s] = Math.round(((210 + (i * 360) / n) % 360) * 10) / 10
  })
  return map
}
