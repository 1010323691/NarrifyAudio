import { reactive } from 'vue'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { useWorkbenchScope, withinScope } from './useWorkbenchScope'

export function useWorkbenchTaskControl() {
  const tasks = useTaskStore()
  const { push: toast } = useToast()
  const capture = useWorkbenchScope()
  const pending = reactive<Record<string, boolean>>({})
  async function control(id: string, action: 'cancel' | 'retry') {
    if (pending[id]) return
    const current = capture()
    pending[id] = true
    try {
      await withinScope(tasks.control(id, action), current)
      toast({
        title: action === 'retry' ? '已提交重试' : '已请求取消',
        description: '任务状态会自动更新。',
      })
    } catch (error: any) {
      if (current())
        toast({
          title: '任务操作失败',
          variant: 'destructive',
          description: error?.message?.includes('最大尝试次数')
            ? `${error.message}。请修复输入后，重新选择章节并提交制作任务。`
            : error?.message || '请稍后重试',
        })
    } finally {
      delete pending[id]
    }
  }
  return { control, pending }
}
