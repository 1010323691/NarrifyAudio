// Single source for task-type prefix knowledge (S8/Q11). Mirrors the backend
// canonical registry in backend/platform/task_types.py — when a new task type
// lands there, extend TASK_MODULES here and nowhere else.
//
// Consumers: taskLabels (module label lookup), ProjectOverview stage filtering,
// Admin service metrics. Prefix sets are disjoint, so first-match scanning is
// order-insensitive.

export interface TaskModuleDef {
  key: string
  label: string
  prefixes: string[]
}

export const TASK_MODULES: TaskModuleDef[] = [
  { key: 'text', label: '文本排版', prefixes: ['text.'] },
  { key: 'book', label: '排版与分册', prefixes: ['book.'] },
  { key: 'script', label: '文本解析', prefixes: ['script.'] },
  { key: 'voices', label: '角色配音', prefixes: ['voices.'] },
  { key: 'tts', label: '音频合成', prefixes: ['tts.batch', 'tts.reset'] },
  { key: 'merge', label: '音频合并', prefixes: ['tts.merge'] },
  { key: 'audio', label: '音频分集', prefixes: ['audio.'] },
  { key: 'bgm', label: '背景音乐', prefixes: ['bgm.'] },
  { key: 'music', label: '音乐库', prefixes: ['music.'] },
]

/** The module a task type belongs to, or undefined for unknown/other types. */
export function taskModuleOf(taskType: string): TaskModuleDef | undefined {
  return TASK_MODULES.find((mod) => mod.prefixes.some((prefix) => taskType.startsWith(prefix)))
}

/** Display label for a task type (unknown types fall back to 项目任务). */
export function taskModuleLabel(taskType: string): string {
  return taskModuleOf(taskType)?.label ?? '项目任务'
}

/** Fine-grained module key used by the durable task snapshots and project views. */
export function taskModuleKey(taskType: string): string {
  const exact: Record<string, string> = {
    'voices.foundation': 'voices-foundation',
    'voices.clone': 'voices-clone',
    'tts.batch': 'tts-batch',
    'tts.merge': 'merge',
    'bgm.analysis': 'bgm-analysis',
    'bgm.segment': 'bgm-segment',
    'bgm.mix': 'bgm-mix',
    'music.suggest_tags': 'music-ai-tags',
  }
  return exact[taskType] ?? taskModuleOf(taskType)?.key ?? taskType.split('.', 1)[0]
}

/** All prefixes belonging to the named modules; unknown keys contribute none. */
export function modulePrefixes(...keys: string[]): string[] {
  return keys.flatMap((key) => TASK_MODULES.find((mod) => mod.key === key)?.prefixes ?? [])
}
