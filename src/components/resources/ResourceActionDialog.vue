<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { X, Package, Trash2, LoaderCircle, AlertTriangle } from 'lucide-vue-next'
import type { CleanupPreview, ResourceEntry, ResourceQuery } from '@/api/resources'
import { resourceBytes } from '@/utils/resources'
import Pager from '@/views/textformat/Pager.vue'

const props = defineProps<{
  mode: 'export' | 'cleanup'
  submitting: boolean
  exportAll: boolean
  exportFiles: ResourceEntry[]
  exportCount: number
  exportBytes: number
  exportScope: ResourceQuery | null
  cleanup: CleanupPreview | null
  cleanupLoading: boolean
  cleanupPage: number
  cleanupPageSize: number
}>()
const emit = defineEmits<{ close: []; export: []; cleanup: []; 'cleanup-page': [page: number, size?: number] }>()
const panel = ref<HTMLElement | null>(null)
const previousFocus = document.activeElement as HTMLElement | null
const eligibleCleanup = computed(() => props.cleanup?.projects.filter(item => item.count && !item.blocked && item.snapshot_id) || [])
const eligibleBytes = computed(() => eligibleCleanup.value.reduce((sum, item) => sum + item.size_bytes, 0))
const eligibleCount = computed(() => eligibleCleanup.value.reduce((sum, item) => sum + item.count, 0))
function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && !props.submitting) { event.preventDefault(); emit('close'); return }
  if (event.key !== 'Tab' || !panel.value) return
  const elements = [...panel.value.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input, select, summary')]
  const first = elements[0], last = elements[elements.length - 1]
  if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.value)) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
watch(() => props.mode, async () => { await nextTick(); panel.value?.querySelector<HTMLButtonElement>('[data-cancel-action]')?.focus() }, { immediate: true })
onBeforeUnmount(() => { if (previousFocus?.isConnected) previousFocus.focus() })
</script>

<template>
  <Teleport to="body"><div class="rc-action-host" @keydown="onKeydown" @click.self="!submitting && emit('close')"><section ref="panel" class="rc-action-panel" role="dialog" aria-modal="true" aria-labelledby="rc-action-title" tabindex="-1">
    <header class="rc-action-header"><span class="rc-action-icon" :class="{ 'is-cleanup': mode === 'cleanup' }"><Package v-if="mode === 'export'" :size="22" /><Trash2 v-else :size="22" /></span><div><h2 id="rc-action-title">{{ mode === 'export' ? '批量下载成品' : '清理过期临时缓存' }}</h2><p>{{ mode === 'export' ? '只打包已完成的音频成品，保留目录结构。' : '先核对范围，执行时还会重新检查文件与任务。' }}</p></div><button class="rc-icon-button" aria-label="关闭确认" :disabled="submitting" @click="emit('close')"><X :size="19" /></button></header>
    <div v-if="mode === 'export'" class="rc-action-content"><dl class="rc-action-details"><dt>导出范围</dt><dd>{{ exportAll ? (exportScope?.path ? '当前目录及子目录的匹配成品' : '当前范围的全部匹配成品') : '本页选择的成品' }}</dd><dt>文件数量</dt><dd>{{ exportCount }}个文件</dd><dt>源文件大小</dt><dd>{{ resourceBytes(exportBytes) }}</dd><dt>打包结果</dt><dd>ZIP，保留相对目录，同名文件不覆盖</dd><dt>下载期限</dt><dd>完成后保留7天</dd></dl><div class="rc-export-exclusions">不包含章节文本、解析结果、合成片段、角色资料、配置、日志及临时缓存。</div><p class="rc-action-hint">打包通过后台任务完成，可离开页面继续制作。此包为成品合集，可用于交付或保存。</p></div>
    <div v-else class="rc-action-content"><div v-if="cleanupLoading && !cleanup" class="rc-preview-loading"><LoaderCircle :size="22" class="rc-spin" />正在读取清理候选…</div><template v-if="cleanup"><div class="rc-cleanup-total"><span>可清理 <strong>{{ eligibleCount }}</strong>项</span><span>预计释放 <strong>{{ resourceBytes(eligibleBytes) }}</strong></span></div><div class="rc-cleanup-project-list"><div v-for="item in cleanup.projects" :key="item.project_id" class="rc-cleanup-project"><div><strong>{{ item.name }}</strong><span>{{ item.blocked ? '项目有活动任务，执行时将跳过' : `${item.count}项 · ${resourceBytes(item.size_bytes)}` }}</span></div><p v-if="!item.complete" class="rc-warning-text">清单不完整，只检查已读取候选。</p><div class="rc-cleanup-directory-list"><span v-for="directory in item.directories" :key="directory.module">{{ directory.module }}/ · {{ directory.count }}项</span></div></div></div><details class="rc-cleanup-files"><summary>查看候选文件明细（{{ cleanup.total }}项）</summary><div v-for="item in cleanup.items" :key="item.id" class="rc-cleanup-file"><div><span>{{ item.project_name }}</span><code>{{ item.relative_path }}</code></div><span>{{ resourceBytes(item.size_bytes) }}</span></div><Pager class="mt-2" :page="cleanupPage" :page-count="Math.max(1, Math.ceil(cleanup.total / cleanupPageSize))" :total="cleanup.total" :page-size="cleanupPageSize" unit="项" @update:page="emit('cleanup-page', $event)" @update:page-size="emit('cleanup-page', 1, $event)" /></details><div class="rc-notice"><AlertTriangle :size="16" />只处理允许的临时目录中超过7天的普通文件。删除不进入回收站，无法恢复。</div><p class="rc-action-hint">原文、章节、解析、角色资料、正式音频、BGM分析、配置和日志不会被清理。</p></template></div>
    <footer class="rc-action-footer"><button v-if="mode === 'cleanup'" class="rc-button rc-button--quiet" :disabled="submitting || cleanupLoading" @click="emit('cleanup-page', 1)">重新检查资格</button><button data-cancel-action class="rc-button" :disabled="submitting" @click="emit('close')">取消</button><button v-if="mode === 'export'" class="rc-button rc-button--primary" :disabled="submitting || !exportCount" @click="emit('export')"><LoaderCircle v-if="submitting" :size="16" class="rc-spin" /><Package v-else :size="16" />{{ submitting ? '提交中…' : '开始打包' }}</button><button v-else class="rc-button rc-button--danger" :disabled="submitting || !eligibleCount || cleanupLoading" @click="emit('cleanup')"><LoaderCircle v-if="submitting" :size="16" class="rc-spin" /><Trash2 v-else :size="16" />{{ submitting ? '提交中…' : '确认清理' }}</button></footer>
  </section></div></Teleport>
</template>
