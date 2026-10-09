// Chart tokens for the admin console. The categorical order is the validated
// reference palette (adjacent CVD ΔE ≥ 8.4, normal-vision ≥ 19.3 on both the
// admin card surfaces); never cycle past it — fold extra series into "其他".
export interface ChartTokens {
  dark: boolean
  surface: string
  text: string
  textSecondary: string
  muted: string
  grid: string
  axis: string
  series: string[]
  status: { good: string; warning: string; serious: string; critical: string }
  sequential: string[]
  tooltipBg: string
  tooltipBorder: string
}

const LIGHT: ChartTokens = {
  dark: false,
  surface: '#ffffff',
  text: '#1f2633',
  textSecondary: '#4b5363',
  muted: '#7b8494',
  grid: '#eceef2',
  axis: '#cfd4dc',
  series: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948'],
  status: { good: '#0ca30c', warning: '#fab219', serious: '#ec835a', critical: '#d03b3b' },
  sequential: ['#eef4fd', '#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#184f95', '#0d366b'],
  tooltipBg: '#ffffff',
  tooltipBorder: 'rgba(31,38,51,0.12)',
}

const DARK: ChartTokens = {
  dark: true,
  surface: '#1c2029',
  text: '#e8eaee',
  textSecondary: '#b9bfca',
  muted: '#8a92a0',
  grid: '#2a2f39',
  axis: '#3a404c',
  series: ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'],
  status: { good: '#0ca30c', warning: '#fab219', serious: '#ec835a', critical: '#d03b3b' },
  sequential: ['#232936', '#0d366b', '#184f95', '#1c5cab', '#256abf', '#3987e5', '#6da7ec', '#9ec5f4'],
  tooltipBg: '#232834',
  tooltipBorder: 'rgba(255,255,255,0.12)',
}

export function chartTokens(dark: boolean): ChartTokens {
  return dark ? DARK : LIGHT
}

/** Fixed colour per worker group — the entity keeps its colour on every chart. */
export const GROUP_ORDER = ['llm', 'tts', 'audio', 'system', 'worker'] as const
export const GROUP_LABELS: Record<string, string> = { llm: 'LLM', tts: 'TTS', audio: '音频', system: '文本/系统', worker: '其他' }
export function groupColor(tokens: ChartTokens, group: string): string {
  const index = GROUP_ORDER.indexOf(group as typeof GROUP_ORDER[number])
  return tokens.series[index < 0 ? 4 : index]
}
