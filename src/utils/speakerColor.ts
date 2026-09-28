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
