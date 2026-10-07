<script
  setup
  lang="ts"
  generic="T extends { workKey: string; workName: string; workState: string }"
>
import type { ListPagination, ListQuery } from '@/api/listPaging'
import { computed, nextTick, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from 'vue'
import Button from '@/components/ui/Button.vue'
import WorkbenchToolbar from '@/components/WorkbenchToolbar.vue'
import Pager from '@/views/textformat/Pager.vue'
import { X } from 'lucide-vue-next'
import { useWorkbenchDialog } from '@/composables/useWorkbenchDialog'
import { useTenRowHeight } from '@/composables/useTenRowHeight'

const props = defineProps<{
  remote?: boolean
  pagination?: ListPagination
  rows: T[]
  selected: Record<string, boolean>
  filters: { key: string; label: string }[]
  columns: { label: string; width: string; align?: 'left' | 'center' | 'right' }[]
  loading: boolean
  loadError: string
  disabled: boolean
  rowDisabled?: (row: T) => boolean
  overlayOpen?: boolean
  emptyText: string
  label: string
}>()
const emit = defineEmits<{
  requestPage: [query: ListQuery]
  selectScope: [query: ListQuery]
  refresh: []
  select: [key: string, event: Event]
  selectFiltered: [keys: string[]]
}>()
const query = ref('')
const filter = ref('all')
const page = ref(1)
const pageSize = ref(10)
const table = ref<HTMLTableElement | null>(null)
const rowHeight = useTenRowHeight(table, pageSize)
const focusedKey = ref('')
const narrow = ref(false)
const detailOpen = ref(false)
const detailPanel = ref<HTMLElement | null>(null)
const shell = ref<HTMLElement | null>(null)
let media: MediaQueryList | null = null
let returnFocus: HTMLElement | null = null
const filtered = computed(() =>
  props.remote ? props.rows : props.rows.filter(
    (row) =>
      (filter.value === 'all' || row.workState === filter.value) &&
      row.workName.toLocaleLowerCase().includes(query.value.trim().toLocaleLowerCase()),
  ),
)
const pageCount = computed(() => Math.max(1, Math.ceil((props.remote ? props.pagination?.total ?? 0 : filtered.value.length) / pageSize.value)))
const visible = computed(() =>
  props.remote ? filtered.value : filtered.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value),
)
const focused = computed(
  () => filtered.value.find((row) => row.workKey === focusedKey.value) ?? null,
)
const eligible = computed(() => filtered.value.filter((row) => !props.rowDisabled?.(row)))
const modalOpen = computed(() => narrow.value && detailOpen.value)
const trapDetail = useWorkbenchDialog(modalOpen, detailPanel, closeDetail, () => returnFocus)
watch([query, filter], () => {
  page.value = 1
})
watch(pageCount, (count) => {
  page.value = Math.min(page.value, count)
})
watch(
  filtered,
  (items) => {
    if (!items.some((row) => row.workKey === focusedKey.value)) {
      focusedKey.value = items[0]?.workKey ?? ''
      if (!items.length) closeDetail()
    }
  },
  { immediate: true },
)
watch([page, pageSize, query, filter], () => {
  if (props.remote) emit('requestPage', { page: page.value, page_size: pageSize.value, q: query.value, filter: filter.value })
})
function selectScope() {
  if (props.remote) emit('selectScope', { page: 1, page_size: pageSize.value, q: query.value, filter: filter.value })
  else emit('selectFiltered', eligible.value.map(row => row.workKey))
}
function updateNarrow() {
  narrow.value = media?.matches ?? false
  if (!narrow.value) detailOpen.value = false
}
function closeDetail() {
  detailOpen.value = false
  void nextTick().then(() => {
    if (returnFocus?.isConnected) returnFocus.focus()
  })
}
async function inspect(row: T, event: Event) {
  focusedKey.value = row.workKey
  if (!narrow.value) return
  const target = event.currentTarget as HTMLElement
  returnFocus = target.matches('.production-name')
    ? target
    : target.querySelector<HTMLElement>('.production-name')
  detailOpen.value = true
  await nextTick()
  detailPanel.value?.querySelector<HTMLElement>('button')?.focus()
}
function detailKeydown(event: KeyboardEvent) {
  if (props.overlayOpen) return
  trapDetail(event)
}
function inspectRow(row: T, event: MouseEvent) {
  // Embedded controls retain their own action; the rest of the row opens details.
  if ((event.target as HTMLElement).closest('button,a,input,select,textarea,[role="button"]')) return
  void inspect(row, event)
}
function clearFilters() {
  query.value = ''
  filter.value = 'all'
}
function changePageSize(size: number) {
  pageSize.value = size
  page.value = 1
}
onMounted(() => {
  media = window.matchMedia('(max-width:1100px)')
  updateNarrow()
  media.addEventListener('change', updateNarrow)
})
onDeactivated(() => {
  detailOpen.value = false
})
onBeforeUnmount(() => media?.removeEventListener('change', updateNarrow))
</script>

<template>
  <section ref="shell" class="production-workbench" :aria-label="label" :aria-busy="loading">
    <div class="production-list" :inert="narrow && detailOpen ? true : undefined">
      <WorkbenchToolbar
        v-model:query="query"
        v-model:filter="filter"
        :filters="[{ key: 'all', label: '全部', count: remote ? pagination?.counts.all : rows.length }, ...filters.map(item => ({ ...item, count: remote ? pagination?.counts[item.key] : rows.filter(row => row.workState === item.key).length }))]"
        :loading="loading"
        @refresh="emit('refresh')"
      >
        <template #selection>
          <Button
            variant="ghost"
            size="sm"
            :disabled="disabled || loading || !!loadError || (remote ? !pagination?.total : !eligible.length)"
            @click="selectScope"
            >选择筛选结果（{{ remote ? pagination?.total ?? 0 : eligible.length }}）</Button
          >
          <slot name="selection" />
        </template>
      </WorkbenchToolbar>
      <div v-if="loadError" class="production-load-error" role="alert">
        <p>{{ loadError }}</p>
        <p v-if="rows.length" class="mt-1">展示上次已知状态，请刷新后再提交。</p>
        <Button
          variant="outline"
          size="sm"
          class="mt-2"
          :disabled="loading"
          @click="emit('refresh')"
          >重试加载</Button
        >
      </div>
      <div class="production-scroll">
        <table ref="table" class="production-table workbench-table" :class="{ 'workbench-table--fit': pageSize === 10 }" :style="{ '--list-row-height': rowHeight }" :aria-label="label + '列表'">
          <colgroup>
            <col style="width: 32px" />
            <col />
            <col v-for="column in columns" :key="column.label" :style="{ width: column.width }" />
          </colgroup>
          <thead>
            <tr>
              <th><span class="sr-only">选择</span></th>
              <th>文件 / 章节</th>
              <th v-for="column in columns" :key="column.label" :style="{ textAlign: column.align }">{{ column.label }}</th>
            </tr>
          </thead>
          <tbody v-if="loading && !rows.length">
            <tr v-for="i in 10" :key="i" aria-hidden="true">
              <td :colspan="columns.length + 2">
                <div class="h-4 rounded bg-muted motion-safe:animate-pulse" />
              </td>
            </tr>
          </tbody>
          <tbody v-else>
            <tr
              v-for="row in visible"
              :key="row.workKey"
              :class="{ 'production-focused': focusedKey === row.workKey }"
              @click="inspectRow(row, $event)"
            >
              <td>
                <input
                  type="checkbox"
                  class="h-3.5 w-3.5 accent-primary"
                  :aria-label="'选择 ' + row.workName"
                  :checked="!!selected[row.workKey]"
                  :disabled="disabled || loading || !!loadError || rowDisabled?.(row)"
                  @change="emit('select', row.workKey, $event)"
                />
              </td>
              <td>
                <button
                  type="button"
                  class="production-name"
                  :title="row.workName"
                  :aria-pressed="focusedKey === row.workKey"
                  @click="inspect(row, $event)"
                >
                  {{ row.workName }}</button
                ><slot name="subtitle" :row="row" />
              </td>
              <slot name="cells" :row="row" />
            </tr>
          </tbody>
        </table>
        <div v-if="!loading && !filtered.length && !loadError" class="production-empty">
          <p>{{ rows.length ? '没有匹配的文件或章节' : emptyText }}</p>
          <Button
            v-if="rows.length"
            variant="ghost"
            size="sm"
            class="mt-2"
            @click="clearFilters"
            >清除筛选</Button
          ><slot v-else name="empty" />
        </div>
      </div>
      <Pager
        class="border-t px-3 py-2"
        :page="page"
        :page-count="pageCount"
        :total="remote ? pagination?.total ?? 0 : filtered.length"
        :page-size="pageSize"
        :page-size-options="[10, 20, 50]"
        unit="项"
        @update:page="page = $event"
        @update:page-size="changePageSize"
      />
    </div>
    <div v-if="narrow && detailOpen" class="production-backdrop" @click="closeDetail" />
    <aside
      ref="detailPanel"
      class="production-detail"
      :class="{ 'production-detail-open': detailOpen }"
      :inert="overlayOpen || undefined"
      :role="narrow ? 'dialog' : undefined"
      :aria-modal="narrow && detailOpen && !overlayOpen ? true : undefined"
      :aria-label="label + '详情'"
      @keydown="detailKeydown"
    >
      <Button
        v-if="narrow"
        variant="ghost"
        class="mb-2 ml-auto flex h-8 w-8 p-0"
        aria-label="关闭详情"
        @click="closeDetail"
        ><X class="h-4 w-4"
      /></Button>
      <div v-if="focused" :key="focused.workKey" class="space-y-4">
        <div>
          <p class="text-[11px] text-muted-foreground">当前{{ label }} · 详情</p>
          <h2 class="mt-1 break-all text-sm font-semibold">{{ focused.workName }}</h2>
        </div>
        <slot name="detail" :row="focused" />
      </div>
      <p v-else class="production-empty">选择列表中的名称，检查条件与结果。</p>
    </aside>
  </section>
</template>

<style>
.production-workbench {
  display: grid;
  min-height: 0;
  grid-template-columns: minmax(0, 1fr) 34%;
  border: 1px solid hsl(var(--border));
  border-radius: 12px;
  background: hsl(var(--card) / 0.95);
  overflow: hidden;
  box-shadow: var(--glass-shadow);
}
.production-list {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  border-right: 1px solid hsl(var(--border));
}
.production-scroll {
  flex: 1;
  overflow: auto;
  min-height: 0;
}
.production-table {
  width: 100%;
  table-layout: fixed;
  font-size: 12px;
  line-height: 18px;
  border-collapse: collapse;
}
.production-table td { overflow-wrap:anywhere; }
.production-table tr:hover {
  background: hsl(var(--muted) / 0.5);
}
.production-table .production-focused {
  background: hsl(var(--primary) / 0.08);
  box-shadow: inset 3px 0 hsl(var(--primary));
}
.production-table tbody tr:not([aria-hidden]) {
  cursor: pointer;
}
.production-table tbody tr:not([aria-hidden]):hover {
  background: hsl(var(--primary) / 0.05);
}
.production-table tbody tr.production-focused:hover {
  background: hsl(var(--primary) / 0.1);
}
.production-name {
  display: block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
  font-weight: 600;
}
.production-detail {
  min-width: 0;
  min-height: 0;
  overflow: auto;
  padding: 16px;
}
.production-empty {
  padding: 40px 16px;
  text-align: center;
  font-size: 12px;
  color: hsl(var(--muted-foreground));
}
.production-load-error {
  padding: 10px 12px;
  color: hsl(var(--destructive));
  font-size: 12px;
  border-bottom: 1px solid hsl(var(--border));
}
.production-facts {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 8px;
  font-size: 12px;
}
.production-facts dt {
  color: hsl(var(--muted-foreground));
}
.production-facts dd {
  font-variant-numeric: tabular-nums;
  text-align: right;
}
.production-section {
  border-top: 1px solid hsl(var(--border));
  padding-top: 12px;
  font-size: 12px;
}
.production-section h3 {
  font-weight: 600;
  margin-bottom: 8px;
}
.production-workbench button:focus-visible,
.production-workbench input:focus-visible,
.production-workbench select:focus-visible {
  outline: 2px solid hsl(var(--ring));
  outline-offset: 2px;
}
@media (max-width: 1100px) {
  .production-workbench {
    grid-template-columns: 1fr;
  }
  .production-list {
    border-right: 0;
  }
  .production-detail {
    display: none;
    position: fixed;
    inset: 0 0 0 auto;
    z-index: 40;
    width: 85vw;
    max-width: 420px;
    max-height: none;
    background: hsl(var(--background));
    border-left: 1px solid hsl(var(--border));
    box-shadow: var(--glass-shadow-strong);
  }
  .production-detail-open {
    display: block;
  }
  .production-backdrop {
    position: fixed;
    inset: 0;
    z-index: 39;
    background: hsl(var(--foreground) / 0.4);
  }
}
@media (max-width: 1100px) {
  .production-detail {
    max-height: none;
  }
}
@media (prefers-reduced-motion: reduce) {
  .production-workbench *,
  .workbench-actionbar * {
    animation: none !important;
    transition: none !important;
  }
}
</style>
