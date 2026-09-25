import { computed, type ComputedRef } from 'vue'
import { useTaskStore } from '@/stores/task'
import type { TaskSnapshot } from '@/types'

// 任务按 label 尾部「：{key}」派生归位（单源，S8/Q11）：
// 三个视图（Merge 包名 / BGM 章节 stem / 音乐库曲目名）原先各内联一份同形算法，
// 终态集合与取 key 口径也与 SSE 已处理标记块共享。

/** 终态口径：重试走同一任务 id，同一任务不会同时在途与失败两个集合里。
 * timeout 不在此集合——「超时」行按在途展示，与原有内联副本一致。 */
export const LABEL_TASK_TERMINAL_STATUSES = new Set(['cancelled', 'succeeded', 'failed'])

/** label 第一个「：」之后的 key；口径与后端 re.search(r"：(.+)$") 相同。 */
export function labelKeyOf(label: string): string {
  const i = label.indexOf('：')
  return i >= 0 ? label.slice(i + 1) : ''
}

/**
 * 从 store 全量任务派生按 key 归位的行任务映射（module：单一模块名或模块名集合）。
 * 在途（非终态）取 seq 升序首个；失败取 seq 降序最新。
 */
export function useLabelDerivedTasks(
  module: string | string[],
): ComputedRef<{ active: Map<string, TaskSnapshot>; failed: Map<string, TaskSnapshot> }> {
  const taskStore = useTaskStore()
  const modules = new Set(Array.isArray(module) ? module : [module])
  return computed(() => {
    const active = new Map<string, TaskSnapshot>()
    const failed = new Map<string, TaskSnapshot>()
    for (const t of taskStore.tasks) {
      if (!modules.has(t.module)) continue
      const key = labelKeyOf(t.label)
      if (!key) continue
      if (t.status === 'failed') {
        const cur = failed.get(key)
        if (!cur || t.seq > cur.seq) failed.set(key, t)
      } else if (!LABEL_TASK_TERMINAL_STATUSES.has(t.status)) {
        const cur = active.get(key)
        if (!cur || t.seq < cur.seq) active.set(key, t)
      }
    }
    return { active, failed }
  })
}
