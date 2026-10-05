export const TASK_CENTER_CATEGORIES = [
  { id: 'script', label: '文本解析', description: '解析书籍文本并生成角色脚本' },
  { id: 'voices-foundation', label: '角色配音－生成语音推理基础', description: '为角色生成语音推理基础' },
  { id: 'voices-clone', label: '角色配音－制作克隆音频', description: '制作角色克隆音频' },
  { id: 'tts', label: '音频合成', description: '将脚本批量合成为语音' },
  { id: 'preview', label: '整章预览', description: '预览章节最终合成并逐句微调' },
  { id: 'merge', label: '音频合并', description: '合并章节音频' },
  { id: 'bgm', label: '背景音乐混音', description: '处理背景音乐并完成混音' },
  { id: 'resources', label: '资源管理', description: '扫描资源、打包文件和清理过期缓存' },
] as const

export type TaskCenterCategoryId = (typeof TASK_CENTER_CATEGORIES)[number]['id']

export function taskCenterCategoryOf(taskType: string): TaskCenterCategoryId | undefined {
  if (taskType === 'script.parse') return 'script'
  if (taskType === 'voices.foundation') return 'voices-foundation'
  if (taskType === 'voices.clone') return 'voices-clone'
  if (taskType === 'tts.batch') return 'tts'
  if (taskType === 'tts.preview_render') return 'preview'
  if (taskType === 'tts.merge') return 'merge'
  if (['bgm.analysis', 'bgm.segment', 'bgm.match', 'bgm.mix', 'bgm.package'].includes(taskType)) return 'bgm'
  if (taskType.startsWith('resources.')) return 'resources'
  return undefined
}
