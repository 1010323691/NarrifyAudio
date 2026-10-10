// Chart tokens for the admin console. Values are CSS colour expressions that
// resolve against the `.admin-shell` variables (admin.css), so light/dark
// follows the `.dark` class with no JS. The categorical order is the validated
// reference palette; never cycle past it — fold extra series into "其他".
export interface ChartTokens {
  muted: string
  axis: string
  series: string[]
  status: { good: string; warning: string; serious: string; critical: string }
  /** Light step of the primary hue, for "free / empty" slices. */
  sequential: string[]
}

const TOKENS: ChartTokens = {
  muted: 'hsl(var(--muted-foreground))',
  axis: 'hsl(var(--input))',
  series: Array.from({ length: 8 }, (_, index) => `var(--viz-${index + 1})`),
  status: {
    good: 'var(--status-good)', warning: 'var(--status-warning)',
    serious: 'var(--status-serious)', critical: 'var(--status-critical)',
  },
  sequential: [18, 30, 45, 60, 72, 82, 91, 100].map(pct => `color-mix(in srgb, var(--viz-1) ${pct}%, hsl(var(--card)))`),
}

export function chartTokens(): ChartTokens {
  return TOKENS
}

/** Fixed colour per worker group — the entity keeps its colour on every chart. */
export const GROUP_ORDER = ['llm', 'tts', 'audio', 'system', 'worker'] as const
export const GROUP_LABELS: Record<string, string> = { llm: 'LLM', tts: 'TTS', audio: '音频', system: '文本/系统', worker: '其他' }
export function groupColor(tokens: ChartTokens, group: string): string {
  const index = GROUP_ORDER.indexOf(group as typeof GROUP_ORDER[number])
  return tokens.series[index < 0 ? 4 : index]
}
