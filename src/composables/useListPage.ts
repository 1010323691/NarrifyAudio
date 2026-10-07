import { onBeforeUnmount, onActivated, onDeactivated, reactive, ref, watch, type Ref } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'
import type { ListPagination, ListQuery } from '@/api/listPaging'

/** A page is a view, never a batch-operation scope. */
export function useListPage(refresh: () => unknown, reset?: () => void) {
  const query = reactive<ListQuery>({ page: 1, page_size: 10, q: '', filter: 'all' })
  const pagination = ref<ListPagination>()
  let active = true
  let controller: AbortController | null = null
  function begin() {
    controller?.abort()
    controller = new AbortController()
    if (!active) controller.abort()
    return controller.signal
  }
  function cancel() { controller?.abort(); controller = null }
  function request(next: ListQuery) { Object.assign(query, next); void refresh() }
  function received(meta?: ListPagination, externalPage?: Ref<number>) {
    pagination.value = meta
    if (meta && externalPage) {
      const last = Math.max(1, Math.ceil(meta.total / meta.page_size))
      if (externalPage.value > last) externalPage.value = last
      return
    }
    if (meta && query.page! > Math.max(1, Math.ceil(meta.total / query.page_size!))) {
      query.page = Math.max(1, Math.ceil(meta.total / query.page_size!))
      void refresh()
    }
  }
  const auth = useAuthStore(), project = useProjectStore()
  watch([() => auth.user?.id, () => project.activeProjectId], () => {
    cancel(); reset?.(); pagination.value = undefined; Object.assign(query, { page: 1, q: '', filter: 'all' })
  }, { flush: 'sync' })
  onActivated(() => { active = true })
  onDeactivated(() => { active = false; cancel() })
  onBeforeUnmount(() => { active = false; cancel() })
  return { query, pagination, begin, cancel, request, received }
}
