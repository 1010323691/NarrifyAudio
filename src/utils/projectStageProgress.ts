export function stageProgressColor(percent: number) {
  const value = Math.max(0, Math.min(100, percent)) / 100
  // RGB interpolation follows the short red → magenta → violet path.
  return `${Math.round(239 + (124 - 239) * value)} ${Math.round(68 + (92 - 68) * value)} ${Math.round(68 + (246 - 68) * value)}`
}

/** Preview readiness is measured against the dialogue already parsed, not listening history. */
export function previewCompletion(value: { completed: number; total: number; unit: string; percent: number | null }) {
  return { ...value, percent: value.total > 0 ? Math.min(100, Math.floor(value.completed / value.total * 100)) : 0 }
}
