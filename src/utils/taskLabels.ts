/** Human-readable task labels for user-facing project views. */
export function taskTypeLabel(taskType: string): string {
  if (taskType.startsWith('text.')) return '文本排版'
  if (taskType.startsWith('book.')) return '排版与分册'
  if (taskType.startsWith('script.')) return '文本解析'
  if (taskType.startsWith('voices.')) return '角色配音'
  if (taskType.startsWith('tts.batch') || taskType.startsWith('tts.stress') || taskType.startsWith('tts.reset')) return '音频合成'
  if (taskType.startsWith('tts.merge')) return '音频合并'
  if (taskType.startsWith('audio.')) return '音频分集'
  if (taskType.startsWith('bgm.')) return '背景音乐'
  if (taskType.startsWith('music.')) return '音乐库'
  return '项目任务'
}
