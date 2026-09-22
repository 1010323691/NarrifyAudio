import { reactive } from 'vue'

export interface ToastItem {
  id: number
  title: string
  description?: string
  variant?: 'default' | 'destructive' | 'success'
  duration: number
}

// A module-level reactive list: any component can push; the single <Toaster>
// mounted in App.vue renders + auto-dismisses them.
export const toasts = reactive<ToastItem[]>([])
let seq = 0

export function useToast() {
  function push(t: {
    title: string
    description?: string
    variant?: ToastItem['variant']
    duration?: number
  }) {
    const id = ++seq
    toasts.push({ id, duration: 3200, variant: 'default', ...t })
    return id
  }
  function dismiss(id: number) {
    const i = toasts.findIndex((x) => x.id === id)
    if (i !== -1) toasts.splice(i, 1)
  }
  return { push, dismiss }
}
