<script setup lang="ts">
// 批量合并疑似角色：左栏目标角色，右栏指向树。状态全部在 useBatchMerge，这里只负责展示、
// 焦点/Esc 分层（确认层 → 弹窗）以及三种二次确认（合并 / 撤销 / 有未合并的选择时关闭）。
import { computed, nextTick, ref, watch } from 'vue'
import { ArrowRight, Loader2, Search, X } from 'lucide-vue-next'
import Alert from '@/components/ui/Alert.vue'
import Badge from '@/components/ui/Badge.vue'
import Button from '@/components/ui/Button.vue'
import Input from '@/components/ui/Input.vue'
import Switch from '@/components/ui/Switch.vue'
import MiniAudioPlayer from '@/components/ui/MiniAudioPlayer.vue'
import { useToast } from '@/components/ui/toast'
import type { useBatchMerge } from '@/composables/useBatchMerge'
import type { MergeRecord } from '@/types'
import MergeLinkTree from './MergeLinkTree.vue'

const props = defineProps<{
  batch: ReturnType<typeof useBatchMerge>
  scopeLabel: string
  previewUrl: (relative: string) => string
}>()
const emit = defineEmits<{ close: [] }>()
const { push: toast } = useToast()
const b = props.batch

type Confirm = { kind: 'merge' } | { kind: 'undo'; record: MergeRecord } | { kind: 'close' }
const confirm = ref<Confirm | null>(null)
const ignoredOpen = ref(false)
const panel = ref<HTMLElement | null>(null)
const confirmPanel = ref<HTMLElement | null>(null)
const targetList = ref<HTMLElement | null>(null)
let opener: HTMLElement | null = null

const current = computed(() => (b.currentTarget.value ? b.roleOf(b.currentTarget.value) : null))
const tagLabel = { none: '', merged: '已合并', review: '待复核' } as const
const timeOf = (iso: string) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? iso : d.toLocaleString() }

watch(() => b.open.value, async (isOpen) => {
  if (isOpen) {
    opener = document.activeElement as HTMLElement | null
    await nextTick()
    panel.value?.querySelector<HTMLElement>('[data-initial-focus]')?.focus()
  } else if (opener?.isConnected) opener.focus()
})
// 排序后保持当前选中项可见。
watch(() => b.visibleTargets.value.map(e => e.name).join('\n'), async () => {
  await nextTick()
  targetList.value?.querySelector<HTMLElement>('[aria-current="true"]')?.scrollIntoView?.({ block: 'nearest' })
})
let confirmReturn: HTMLElement | null = null
watch(confirm, async (value, previous) => {
  if (value && !previous) confirmReturn = document.activeElement as HTMLElement | null
  await nextTick()
  if (value) confirmPanel.value?.querySelector<HTMLElement>('[data-initial-focus]')?.focus()
  else if (previous && b.open.value && confirmReturn?.isConnected) confirmReturn.focus()
})

function requestClose() {
  if (b.busy.value) return
  if (b.selected.value.size) confirm.value = { kind: 'close' }
  else finishClose()
}
function finishClose() {
  confirm.value = null
  ignoredOpen.value = false
  b.closeDialog()
  emit('close')
}

async function runMerge() {
  const plan = b.plan.value
  const target = b.currentTarget.value
  const result = await b.merge()
  if (!result) return
  confirm.value = null
  const fresh = Object.values(result.rematched).filter(Boolean).length
  toast({
    title: '角色已合并', variant: 'success',
    description: `已将 ${result.sources.length} 个角色并入 ${target}（改写 ${result.replaced} 条台词）${fresh ? `；${fresh} 个角色已重新匹配` : ''}${plan?.orphans.length && !fresh ? '；无主角色暂无新的匹配' : ''}`,
  })
}
async function runUndo(record: MergeRecord) {
  const result = await b.undo(record.id)
  if (!result) return
  confirm.value = null
  toast({ title: '已撤销合并', variant: 'success', description: `${record.source} 已从 ${record.target} 中拆分恢复` })
}
async function onVeto(name: string, target: string) {
  await b.veto(name, target)
}

function layerControls(root: HTMLElement) {
  return [...root.querySelectorAll<HTMLElement>('button:not([disabled]),input:not([disabled]),select:not([disabled]),a[href]')]
    .filter(el => el.getClientRects().length > 0)
}
function onKeydown(event: KeyboardEvent) {
  const layer = confirm.value ? confirmPanel.value : panel.value
  if (!layer) return
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    if (b.busy.value) return
    if (confirm.value) confirm.value = null
    else requestClose()
    return
  }
  if (event.key !== 'Tab') return
  const controls = layerControls(layer)
  if (!controls.length) return
  const first = controls[0], last = controls[controls.length - 1]
  if (!layer.contains(document.activeElement) || (event.shiftKey && document.activeElement === first)) {
    event.preventDefault()
    ;(event.shiftKey ? last : first).focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}
function onListKeydown(event: KeyboardEvent) {
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
  const items = [...(targetList.value?.querySelectorAll<HTMLElement>('button.merge-target') ?? [])]
  const index = items.indexOf(document.activeElement as HTMLElement)
  if (index < 0) return
  event.preventDefault()
  items[Math.max(0, Math.min(items.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)))]?.focus()
}
</script>

<template>
  <Teleport to="body">
    <div v-if="batch.open.value" class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" @keydown="onKeydown">
      <div
        ref="panel" role="dialog" aria-label="批量合并疑似角色" :aria-modal="confirm ? undefined : true" :inert="confirm ? true : undefined"
        class="merge-dialog flex w-[min(960px,calc(100vw-2rem))] flex-col overflow-hidden rounded-lg border bg-background shadow-lg"
      >
        <header class="flex items-start justify-between gap-3 border-b px-5 py-3">
          <div>
            <h2 class="text-lg font-semibold">批量合并疑似角色 <span class="ml-2 text-xs font-normal text-muted-foreground">范围：{{ scopeLabel }}</span></h2>
            <p class="mt-1 text-xs text-muted-foreground">选择目标角色，勾选要并入它的疑似角色；“忽略”表示不是同一人。</p>
          </div>
          <Button variant="ghost" size="icon" aria-label="关闭批量合并" :disabled="batch.busy.value" @click="requestClose"><X class="h-4 w-4" /></Button>
        </header>

        <div v-if="batch.loading.value" class="flex flex-1 items-center justify-center gap-2 p-10 text-sm text-muted-foreground"><Loader2 class="h-4 w-4 animate-spin" />正在加载…</div>
        <div v-else-if="!batch.graph.value" class="p-6"><Alert variant="destructive">{{ batch.error.value || '加载失败，请重试。' }}</Alert>
          <Button variant="outline" class="mt-3" @click="batch.openDialog()">重试</Button></div>
        <div v-else class="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-[280px_minmax(0,1fr)]">
          <!-- 左栏：目标角色 -->
          <aside class="hidden min-h-0 flex-col border-r md:flex" aria-label="目标角色">
            <div class="space-y-2 border-b p-3">
              <div class="relative">
                <Search class="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                <Input v-model="batch.query.value" class="h-9 pl-8 text-sm" placeholder="搜索角色…" aria-label="搜索目标角色" data-initial-focus />
              </div>
              <label class="flex items-center justify-between text-xs">只看待处理<Switch v-model="batch.onlyPending.value" small /></label>
            </div>
            <div ref="targetList" class="min-h-0 flex-1 overflow-y-auto p-2" @keydown="onListKeydown">
              <button
                v-for="entry in batch.visibleTargets.value" :key="entry.name" type="button" class="merge-target"
                :class="{ 'is-current': batch.currentTarget.value === entry.name }" :aria-current="batch.currentTarget.value === entry.name || undefined"
                @click="batch.selectTarget(entry.name)"
              >
                <span class="min-w-0 flex-1 truncate">{{ entry.name }}</span>
                <span class="shrink-0 text-[11px] text-muted-foreground">待合并 {{ entry.pending }}</span>
                <Badge v-if="entry.tag !== 'none'" :variant="entry.tag === 'review' ? 'warning' : 'secondary'">{{ tagLabel[entry.tag] }}</Badge>
              </button>
              <p v-if="!batch.visibleTargets.value.length" class="px-2 py-4 text-center text-xs text-muted-foreground">未找到匹配角色</p>
            </div>
          </aside>

          <!-- 右栏 -->
          <section class="flex min-h-0 flex-col" aria-label="指向链">
            <div class="border-b p-3 md:hidden">
              <select class="h-9 w-full rounded-md border bg-background px-2 text-sm" aria-label="目标角色" data-initial-focus :value="batch.currentTarget.value ?? ''" @change="batch.selectTarget(($event.target as HTMLSelectElement).value)">
                <option value="" disabled>请先选择角色</option>
                <option v-for="entry in batch.targets.value" :key="entry.name" :value="entry.name">{{ entry.name }}（待合并 {{ entry.pending }}）</option>
              </select>
            </div>
            <p v-if="!current" class="p-10 text-center text-sm text-muted-foreground">请先在左侧选择一个角色</p>
            <template v-else>
              <div class="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
                <div class="rounded-lg border bg-muted/30 p-3">
                  <div class="flex items-center justify-between gap-2">
                    <div class="min-w-0"><strong class="text-sm">{{ current.name }}</strong>
                      <span class="ml-2 text-xs text-muted-foreground">{{ current.gender === 'male' ? '男' : current.gender === 'female' ? '女' : '性别未知' }} · {{ current.line_count }} 句</span></div>
                    <MiniAudioPlayer v-if="current.preview" :src="previewUrl(current.preview)" class="shrink-0" />
                  </div>
                  <p v-if="current.sample" class="mt-1 text-xs text-muted-foreground">「{{ current.sample }}」</p>
                </div>

                <Alert v-if="batch.newCount.value && !batch.alertDismissed.value" variant="warning">
                  {{ current.name }} 的候选合并项有变化，新增 {{ batch.newCount.value }} 个，请重新处理
                  <template #description><button type="button" class="text-xs underline" @click="batch.alertDismissed.value = true">关闭提醒</button></template>
                </Alert>
                <Alert v-if="batch.error.value" variant="destructive">{{ batch.error.value }}</Alert>

                <MergeLinkTree
                  v-if="batch.rows.value.length" :rows="batch.rows.value" :selected="batch.selected.value" :busy="batch.busy.value" :preview-url="previewUrl"
                  @toggle="batch.toggleSelect" @expand="batch.toggleExpand" @veto="onVeto"
                />
                <p v-else class="py-6 text-center text-sm text-muted-foreground">暂无待合并角色</p>

                <section v-if="batch.records.value.length" aria-label="已合并">
                  <h3 class="mb-1 text-xs font-medium text-muted-foreground">已合并</h3>
                  <ul class="space-y-1">
                    <li v-for="record in batch.records.value" :key="record.id" class="flex items-center gap-2 rounded border px-2 py-1.5 text-xs">
                      <span class="min-w-0 flex-1"><strong class="text-sm">{{ record.source }}</strong><span class="ml-2 text-muted-foreground">{{ record.line_count }} 句 · {{ timeOf(record.merged_at) }}</span></span>
                      <Button variant="outline" size="sm" class="text-xs" :disabled="batch.busy.value || !record.undoable" :title="record.undoable ? '' : record.reason" @click="confirm = { kind: 'undo', record }">撤销</Button>
                    </li>
                  </ul>
                </section>

                <section v-if="batch.vetoes.value.length" aria-label="已忽略">
                  <button type="button" class="text-xs font-medium text-muted-foreground" :aria-expanded="ignoredOpen" @click="ignoredOpen = !ignoredOpen">已忽略（{{ batch.vetoes.value.length }}）{{ ignoredOpen ? '▾' : '▸' }}</button>
                  <ul v-if="ignoredOpen" class="mt-1 space-y-1">
                    <li v-for="veto in batch.vetoes.value" :key="veto.source + veto.target" class="flex items-center gap-2 rounded border px-2 py-1.5 text-xs">
                      <span class="min-w-0 flex-1">{{ veto.source }} 不是 {{ veto.target }}</span>
                      <Button variant="ghost" size="sm" class="text-xs" :disabled="batch.busy.value" @click="batch.restore(veto.source, veto.target)">恢复提示</Button>
                    </li>
                  </ul>
                </section>
              </div>
              <footer class="flex items-center justify-end gap-2 border-t p-3">
                <Button v-if="batch.selected.value.size" variant="ghost" size="sm" :disabled="batch.busy.value" @click="batch.clearSelection()">清除选择</Button>
                <Button :disabled="batch.busy.value || !batch.selected.value.size" @click="confirm = { kind: 'merge' }">合并（{{ batch.selected.value.size }}）</Button>
              </footer>
            </template>
          </section>
        </div>
      </div>

      <!-- 二次确认层：合并 / 撤销 / 关闭 -->
      <div v-if="confirm" class="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4">
        <div ref="confirmPanel" role="dialog" aria-modal="true" :aria-label="confirm.kind === 'merge' ? '确认合并' : confirm.kind === 'undo' ? '确认撤销' : '确认关闭'" class="w-full max-w-md space-y-3 rounded-lg border bg-background p-5 shadow-lg">
          <template v-if="confirm.kind === 'merge' && batch.plan.value">
            <h2 class="text-lg font-semibold">确认合并？</h2>
            <p class="text-sm">将 <strong>{{ batch.plan.value.items.map(i => i.name).join('、') }}</strong> 合并到 <strong>{{ batch.currentTarget.value }}</strong></p>
            <ul class="max-h-48 space-y-1 overflow-y-auto text-xs">
              <li v-for="item in batch.plan.value.items" :key="item.name" class="flex items-center gap-2"><ArrowRight class="h-3 w-3" />{{ item.name }}<Badge v-if="item.indirect" variant="outline">间接</Badge><span class="ml-auto text-muted-foreground">{{ item.lineCount }} 句</span></li>
            </ul>
            <p class="text-xs text-muted-foreground">共改写 {{ batch.plan.value.lines }} 条台词；删除 {{ batch.plan.value.voiceConfigs }} 个角色的声音配置；相关已合成音频将标记为需重新合成。</p>
            <Alert v-if="batch.plan.value.cloned.length" variant="warning">{{ batch.plan.value.cloned.join('、') }} 已有克隆音色，合并后其声音配置将被删除（音频文件保留，撤销合并可恢复）</Alert>
            <Alert v-if="batch.plan.value.orphans.length" variant="info">{{ batch.plan.value.orphans.join('、') }} 的指向对象将被合并，系统将为它们重新匹配</Alert>
          </template>
          <template v-else-if="confirm.kind === 'undo'">
            <h2 class="text-lg font-semibold">确认撤销？</h2>
            <p class="text-sm">将 <strong>{{ confirm.record.source }}</strong> 从 <strong>{{ confirm.record.target }}</strong> 中拆分恢复，是否确认？</p>
          </template>
          <template v-else-if="confirm.kind === 'close'">
            <h2 class="text-lg font-semibold">确认关闭？</h2>
            <p class="text-sm">有 {{ batch.selected.value.size }} 个已选角色尚未合并，确认关闭？</p>
          </template>
          <Alert v-if="batch.error.value" variant="destructive">{{ batch.error.value }}</Alert>
          <div class="flex items-center justify-end gap-2">
            <Button variant="outline" size="sm" :disabled="batch.busy.value" data-initial-focus @click="confirm = null">取消</Button>
            <Button
              :disabled="batch.busy.value"
              @click="confirm.kind === 'merge' ? runMerge() : confirm.kind === 'undo' ? runUndo(confirm.record) : finishClose()"
            ><Loader2 v-if="batch.busy.value" class="h-4 w-4 animate-spin" />确认</Button>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.merge-dialog { height:min(720px, 80vh); }
.merge-target { display:flex; align-items:center; gap:6px; width:100%; padding:8px 10px; border-left:3px solid transparent; border-radius:6px; font-size:13px; text-align:left; }
.merge-target:hover { background:hsl(var(--accent) / .5); }
.merge-target.is-current { border-left-color:hsl(var(--primary)); background:hsl(var(--primary) / .08); font-weight:700; }
button:focus-visible, select:focus-visible { outline:2px solid hsl(var(--ring)); outline-offset:2px; }
</style>
