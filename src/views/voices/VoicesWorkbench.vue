<script setup lang="ts">
import type { ListPagination, ListQuery } from '@/api/listPaging'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { VoiceItem } from '@/types'
import Button from '@/components/ui/Button.vue'
import WorkbenchToolbar from '@/components/WorkbenchToolbar.vue'
import WorkbenchStatus from '@/components/ui/WorkbenchStatus.vue'
import MiniAudioPlayer from '@/components/ui/MiniAudioPlayer.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Pager from '@/views/textformat/Pager.vue'
import { Copy, Loader2, Merge, Users, X } from 'lucide-vue-next'

type PhaseBadge = { label: string; variant: 'default' | 'secondary' | 'destructive' | 'success' | 'warning' | 'outline'; spin: boolean }
const props = defineProps<{
  remote?: boolean
  pagination?: ListPagination
  speakers: VoiceItem[]
  prompts: Record<string, string>
  loading: boolean
  loadError: string
  hasScript: boolean
  foundationBlocked: boolean
  cloneBlocked: boolean
  foundationBusy: boolean
  foundationRunning: boolean
  cloneRunning: boolean
  genderBusy: boolean
  overlayOpen: boolean
  foundationBadge: (v: VoiceItem) => PhaseBadge
  cloneBadge: (v: VoiceItem) => PhaseBadge
  previewUrl: (v: VoiceItem) => string
  pickLabel: (v: VoiceItem) => string
  pickDisabled: (v: VoiceItem) => boolean
}>()
const emit = defineEmits<{
  requestPage: [query: ListQuery]
  refresh: []
  gender: [voice: VoiceItem, element: HTMLElement]
  merge: [voice: VoiceItem]
  pick: [voice: VoiceItem]
  foundation: [voice: VoiceItem]
  clone: [voice: VoiceItem]
  copy: [voice: VoiceItem]
  prompt: [name: string, value: string]
}>()
const query = ref('')
const filter = ref('all')
const page = ref(1)
const pageSize = ref(10)
const selectedName = ref('')
const detailTab = ref('voice')
const narrow = ref(false)
const detailOpen = ref(false)
const detailPanel = ref<HTMLElement | null>(null)
let returnFocus: HTMLElement | null = null
let media: MediaQueryList | null = null
function updateNarrow() { narrow.value = media?.matches ?? false; if (!narrow.value) detailOpen.value = false }
function closeDetail() { detailOpen.value = false; returnFocus?.focus() }
onMounted(() => {
  media = window.matchMedia('(max-width:1100px)')
  updateNarrow()
  media.addEventListener('change', updateNarrow)
})
onBeforeUnmount(() => {
  media?.removeEventListener('change', updateNarrow)
})
const pendingCount = computed(() => props.pagination?.counts.pending ?? props.speakers.filter(v => v.status !== 'ready').length)
const filtered = computed(() => {
  if (props.remote) return props.speakers
  const q = query.value.trim().toLocaleLowerCase()
  return props.speakers.filter(v => (filter.value === 'all' || v.status !== 'ready') &&
    (!q || `${v.name} ${v.alias_of || ''}`.toLocaleLowerCase().includes(q)))
})
const pageCount = computed(() => Math.max(1, Math.ceil((props.remote ? props.pagination?.total ?? 0 : filtered.value.length) / pageSize.value)))
const visible = computed(() => {
  if (props.remote) return filtered.value
  const p = Math.min(page.value, pageCount.value)
  return filtered.value.slice((p - 1) * pageSize.value, p * pageSize.value)
})
const selected = computed(() => props.speakers.find(v => v.name === selectedName.value) ?? null)
watch([query, filter], () => { page.value = 1 })
watch([page, pageSize, query, filter], () => { if (props.remote) emit('requestPage', { page: page.value, page_size: pageSize.value, q: query.value, filter: filter.value }) })
watch(pageCount, n => { page.value = Math.min(page.value, n) })
watch(() => props.speakers, items => {
  if (!items.some(v => v.name === selectedName.value)) selectedName.value = items[0]?.name ?? ''
}, { immediate: true })
async function select(v: VoiceItem, event?: MouseEvent) {
  selectedName.value = v.name
  if (narrow.value) {
    returnFocus = (event?.currentTarget as HTMLElement | null)?.querySelector<HTMLElement>('button') ?? document.activeElement as HTMLElement
    detailOpen.value = true
    await nextTick()
    detailPanel.value?.querySelector<HTMLElement>('button')?.focus()
  }
}
function detailKeydown(event: KeyboardEvent) {
  if (!narrow.value || !detailOpen.value || props.overlayOpen) return
  if (event.key === 'Escape') { event.stopPropagation(); closeDetail() }
  if (event.key !== 'Tab') return
  const controls = [...(detailPanel.value?.querySelectorAll<HTMLElement>('button:not([disabled]),textarea:not([disabled]),input:not([disabled]),a[href]') ?? [])].filter(el => el.getClientRects().length)
  const first = controls[0], last = controls[controls.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
</script>

<template>
  <section class="voice-workbench" aria-label="角色核对工作台" :aria-busy="loading">
    <div class="voice-list">
      <WorkbenchToolbar
        v-model:query="query"
        v-model:filter="filter"
        placeholder="搜索角色或别名"
        :filters="[{ key: 'all', label: '全部', count: remote ? pagination?.counts.all : speakers.length }, { key: 'pending', label: '待完善', count: pendingCount }]"
        :loading="loading"
        @refresh="emit('refresh')"
      />
      <div v-if="loadError" class="voice-load-error" role="alert">
        <p>{{ loadError }}</p><p v-if="speakers.length" class="mt-1 text-muted-foreground">正在展示上次已知状态。</p>
        <Button variant="outline" class="mt-2 h-8" :disabled="loading" @click="emit('refresh')">重试加载</Button>
      </div>
      <div class="voice-scroll">
        <table class="voice-table workbench-table" aria-label="角色状态列表">
          <thead><tr><th>角色 / 别名</th><th class="voice-number">台词</th><th>基础</th><th>音色</th></tr></thead>
          <tbody v-if="loading && !speakers.length">
            <tr v-for="i in pageSize" :key="i" aria-hidden="true"><td colspan="4"><Skeleton class="voice-skeleton h-5" /></td></tr>
          </tbody>
          <tbody v-else>
            <tr v-for="v in visible" :key="v.name" :class="{ 'voice-selected': selectedName === v.name }" @click="select(v, $event)">
              <td><div class="flex min-w-0 items-center gap-2"><span class="voice-sex" :title="v.gender === 'male' ? '男' : v.gender === 'female' ? '女' : '性别未标记'" :class="v.gender === 'male' ? 'bg-sky-500/10 text-sky-700 dark:text-sky-300' : v.gender === 'female' ? 'bg-rose-500/10 text-rose-700 dark:text-rose-300' : 'bg-muted text-muted-foreground'">{{ v.gender === 'male' ? '男' : v.gender === 'female' ? '女' : '未' }}</span><button type="button" class="voice-name" :aria-pressed="selectedName === v.name" :title="v.name" @click.stop="select(v)">{{ v.name }}</button><span v-if="v.alias_of" class="min-w-0 max-w-[40%] truncate text-[11px] text-muted-foreground" :title="`关联提示：${v.alias_of}，确认后可手动合并`">→ {{ v.alias_of }}</span></div></td>
              <td class="voice-number tabular-nums">{{ v.line_count }}</td>
              <td><WorkbenchStatus class="inline-flex items-center" :variant="foundationBadge(v).variant"><Loader2 v-if="foundationBadge(v).spin" class="mr-1 h-3 w-3 animate-spin" />{{ foundationBadge(v).label }}</WorkbenchStatus></td>
              <td><WorkbenchStatus class="inline-flex items-center" :variant="cloneBadge(v).variant"><Loader2 v-if="cloneBadge(v).spin" class="mr-1 h-3 w-3 animate-spin" />{{ cloneBadge(v).label }}</WorkbenchStatus></td>
            </tr>
          </tbody>
        </table>
        <div v-if="!loading && !filtered.length && !loadError" class="voice-empty">
          <Users class="mx-auto mb-2 h-5 w-5 text-muted-foreground" />
          <p>{{ speakers.length ? '没有匹配的角色' : hasScript ? '当前脚本暂无角色' : '尚未检测到解析结果' }}</p>
          <Button v-if="speakers.length" variant="ghost" class="mt-2 h-8" @click="query = ''; filter = 'all'">清除筛选</Button>
          <RouterLink v-else-if="!hasScript" to="/script" class="mt-2 inline-block text-primary underline underline-offset-4">前往文本解析</RouterLink>
        </div>
      </div>
      <Pager
        class="shrink-0 border-t px-4 py-2"
        :page="page"
        :page-count="pageCount"
        :total="remote ? pagination?.total ?? 0 : filtered.length"
        :page-size="pageSize"
        :page-size-options="[10, 20, 50]"
        unit="个角色"
        @update:page="(p: number) => (page = p)"
        @update:page-size="(s: number) => { pageSize = s; page = 1 }"
      />
    </div>
    <div v-if="narrow && detailOpen" class="voice-detail-backdrop" @click="closeDetail" />
    <aside ref="detailPanel" class="voice-detail" :class="{ 'voice-detail-open': detailOpen }" :inert="overlayOpen || undefined" :role="narrow ? 'dialog' : undefined" :aria-modal="narrow && detailOpen && !overlayOpen ? true : undefined" aria-label="角色详情" @keydown="detailKeydown">
      <Button v-if="narrow" variant="ghost" class="mb-2 ml-auto flex h-8 w-8 p-0" aria-label="关闭角色详情" @click="closeDetail"><X class="h-4 w-4" /></Button>
      <template v-if="selected">
        <div class="flex flex-wrap items-start justify-between gap-2">
          <div class="min-w-0 flex-1"><h2 class="break-words text-sm font-semibold">{{ selected.name }}</h2><p class="mt-1 text-[11px] text-muted-foreground">{{ selected.line_count }} 句台词<span v-if="selected.alias_of"> · 关联提示 {{ selected.alias_of }}（需手动合并）</span></p></div>
          <button type="button" class="voice-gender" :disabled="genderBusy || foundationRunning || cloneRunning" aria-label="修改角色性别" @click="emit('gender', selected, $event.currentTarget as HTMLElement)">{{ selected.gender === 'male' ? '男' : selected.gender === 'female' ? '女' : '性别未定' }}</button>
        </div>
        <div class="voice-tabs" role="tablist" aria-label="角色详情">
          <button id="voice-sound-tab" type="button" role="tab" :aria-selected="detailTab === 'voice'" aria-controls="voice-sound-panel" @click="detailTab = 'voice'">声音与音色</button>
          <button id="voice-prompt-tab" type="button" role="tab" :aria-selected="detailTab === 'prompt'" aria-controls="voice-prompt-panel" @click="detailTab = 'prompt'">提示词覆盖</button>
        </div>
        <div v-show="detailTab === 'voice'" id="voice-sound-panel" role="tabpanel" aria-labelledby="voice-sound-tab">
          <h3 class="text-xs font-medium">声音描述</h3><p class="voice-description" tabindex="0" aria-label="声音描述">{{ selected.description || '尚未生成声音描述，请先生成语音推理基础。' }}</p>
          <div class="mb-2 flex items-center justify-between gap-2"><h3 class="text-xs font-medium">当前音色</h3><span class="text-[11px] text-muted-foreground">{{ pickLabel(selected) }}</span></div>
          <div class="voice-player"><MiniAudioPlayer v-if="selected.preview" :key="previewUrl(selected)" :src="previewUrl(selected)" preload-metadata /><span v-else class="text-xs text-muted-foreground">暂无试听音频</span></div>
        </div>
        <div v-show="detailTab === 'prompt'" id="voice-prompt-panel" role="tabpanel" aria-labelledby="voice-prompt-tab">
          <label for="voice-prompt" class="text-xs font-medium">自定义声音描述</label>
          <textarea id="voice-prompt" class="voice-prompt" :value="prompts[selected.name] || ''" :disabled="foundationBusy" placeholder="可选：阶段 1 重新生成时生效" @input="emit('prompt', selected.name, ($event.target as HTMLTextAreaElement).value)" />
          <p class="text-[11px] text-muted-foreground">仅在重新生成此角色的语音推理基础时生效。</p>
          <Button variant="outline" class="mt-2 h-8 text-xs" :disabled="foundationBusy || !selected.description" @click="emit('copy', selected)"><Copy class="h-3 w-3" />复制已有描述</Button>
        </div>
        <div class="mt-3 flex flex-wrap gap-2">
          <Button v-if="detailTab === 'voice'" variant="outline" class="h-8 text-xs" :disabled="pickDisabled(selected)" @click="emit('pick', selected)">选择音色</Button>
          <Button v-if="detailTab === 'voice'" variant="outline" class="h-8 text-xs" :disabled="cloneBlocked || selected.foundation_status !== 'done'" @click="emit('clone', selected)">重新制作</Button>
          <Button variant="outline" class="h-8 text-xs" :disabled="foundationBlocked" @click="emit('foundation', selected)">重新生成基础</Button>
          <Button variant="outline" class="h-8 text-xs" :disabled="foundationRunning || cloneRunning || speakers.length < 2" @click="emit('merge', selected)"><Merge class="h-3 w-3" />合并角色</Button>
        </div>
      </template>
      <p v-else class="voice-empty">{{ loading ? '正在加载角色…' : '选择角色查看声音与音色详情' }}</p>
    </aside>
  </section>
</template>

<style scoped>
.voice-workbench { display:grid; grid-template-columns:minmax(0,1fr) 34%; min-height:390px; border:1px solid hsl(var(--border)); border-radius:12px; background:hsl(var(--card) / .95); overflow:hidden; box-shadow:var(--glass-shadow); }
.voice-list { min-width:0; display:flex; flex-direction:column; border-right:1px solid hsl(var(--border)); }

.voice-scroll { min-height:320px; max-height:440px; overflow:auto; flex:1; }
.voice-table { width:100%; table-layout:fixed; font-size:12px; line-height:18px; border-collapse:collapse; }
.voice-table th:first-child { width:40%; }
.voice-table th:nth-child(2) { width:16%; }
.voice-table tbody tr { cursor:pointer; }
.voice-table tr:hover { background:hsl(var(--muted) / .5); }
.voice-table .voice-selected { background:hsl(var(--primary) / .08); box-shadow:inset 3px 0 hsl(var(--primary)); }
.voice-sex { flex-shrink:0; padding:1px 5px; border-radius:4px; font-size:11px; line-height:18px; }
.voice-number { font-variant-numeric:tabular-nums; }
.voice-name { display:block; min-width:0; flex:1; width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; text-align:left; font-weight:600; }
.voice-detail { min-width:0; max-height:540px; overflow:auto; padding:12px; }
.voice-gender { padding:4px 8px; border:1px solid hsl(var(--border)); border-radius:999px; font-size:11px; }
.voice-tabs { display:flex; gap:18px; margin:10px 0 12px; border-bottom:1px solid hsl(var(--border)); }
.voice-tabs button { padding:8px 0; font-size:12px; border-bottom:2px solid transparent; }
.voice-tabs button[aria-selected=true] { color:hsl(var(--primary)); border-bottom-color:hsl(var(--primary)); }
.voice-description { box-sizing:border-box; height:calc(3.3em + 20px); margin:6px 0 12px; padding:10px; border-radius:8px; background:hsl(var(--muted) / .4); overflow-y:auto; overflow-x:hidden; white-space:pre-wrap; overflow-wrap:anywhere; font-size:12px; line-height:1.65; }
.voice-player { padding:10px; border:1px solid hsl(var(--border)); border-radius:8px; }
.voice-prompt { display:block; width:100%; min-height:120px; margin:6px 0; padding:10px; border:1px solid hsl(var(--input)); border-radius:8px; background:hsl(var(--background)); font-size:12px; resize:vertical; }
.voice-empty { padding:40px 12px; text-align:center; font-size:12px; color:hsl(var(--muted-foreground)); }
.voice-load-error { padding:10px 12px; font-size:12px; color:hsl(var(--destructive)); border-bottom:1px solid hsl(var(--border)); }
button { transition:background-color 150ms ease; }
button:focus-visible, textarea:focus-visible { outline:2px solid hsl(var(--ring)); outline-offset:2px; }
@media(max-width:1100px) { .voice-workbench { grid-template-columns:1fr; } .voice-list { border-right:0; } .voice-detail { display:none; position:fixed; top:0; right:0; bottom:0; z-index:40; width:85vw; max-width:380px; max-height:none; background:hsl(var(--background)); border-left:1px solid hsl(var(--border)); box-shadow:var(--glass-shadow-strong); } .voice-detail-open { display:block; } .voice-detail-backdrop { position:fixed; inset:0; z-index:39; background:rgb(0 0 0 / .4); } }
@media(max-width:480px) { .voice-table td,.voice-table th { padding-left:6px; padding-right:6px; } .voice-table th:first-child { width:34%; } .voice-table th:nth-child(2) { width:16%; } }
@media(pointer:coarse) { button { min-height:44px; } .voice-prompt { font-size:16px; } }
@media(prefers-reduced-motion:reduce) { button { transition:none; } }
</style>
