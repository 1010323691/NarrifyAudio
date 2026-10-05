<script setup lang="ts">
import { computed } from 'vue'
import { Search, Folder, FileText, Music2, Braces, Image, File, ChevronRight, ChevronLeft, Download, Package, X, FolderOpen, ArrowUpRight, Copy } from 'lucide-vue-next'
import type { ResourceEntries, ResourceEntry, ResourceProject } from '@/api/resources'
import { RESOURCE_FILTERS, filterCount, materialCount, resourceBytes, resourceDate } from '@/utils/resources'

const props = defineProps<{
  materials?: boolean
  project: ResourceProject | null
  projects: ResourceProject[]
  projectId: string
  category: string
  path: string
  directoryMode: boolean
  search: string
  extension: string
  sort: string
  page: number
  entries: ResourceEntries | null
  loading: boolean
  error: string
  selection: Set<string>
  allSelected: boolean
  previewId?: string
}>()
const emit = defineEmits<{
  category: [key: string]
  query: [next: Record<string, string | number | undefined>]
  search: [value: string]
  directory: [entry: ResourceEntry]
  'browse-directory': []
  preview: [entry: ResourceEntry]
  select: [id: string]
  'select-page': []
  'clear-selection': []
  export: [all: boolean]
  download: [entry: ResourceEntry]
  copy: [entry: ResourceEntry]
  retry: []
  page: [number: number]
}>()
const filters = computed(() => props.materials ? RESOURCE_FILTERS.filter(item => !['deliverables', 'all', '07_output', 'system'].includes(item.key)) : RESOURCE_FILTERS.filter(item => item.key === 'deliverables'))
const pageCount = computed(() => Math.max(1, Math.ceil((props.entries?.total || 0) / 50)))
const scopeLabel = computed(() => RESOURCE_FILTERS.find(item => item.key === props.category)?.label || '全部业务文件')
const breadcrumbs = computed(() => props.path.split('/').filter(Boolean).map((name, index, parts) => ({ name, path: parts.slice(0, index + 1).join('/') })))
const hasDirectory = computed(() => !!RESOURCE_FILTERS.find(item => item.key === props.category)?.module && !!props.projectId)
function fileIcon(entry: ResourceEntry) {
  if (entry.kind === 'directory') return Folder
  if (entry.preview_kind === 'audio') return Music2
  if (entry.preview_kind === 'image') return Image
  if (entry.preview_kind === 'json' || entry.preview_kind === 'config') return Braces
  return entry.preview_kind === 'text' ? FileText : File
}
</script>

<template>
  <div class="rc-browser">
    <nav class="rc-directory-sidebar" aria-label="资源分类">
      <div class="rc-directory-title">资源分类</div>
      <template v-for="item in filters" :key="item.key">
        <details v-if="item.key === 'system'" class="rc-system-category" :open="category === 'system'"><summary>系统文件<span>配置与日志</span></summary><button class="rc-category" :aria-pressed="category === 'system'" @click="emit('category', 'system')"><span>查看系统文件</span><span>{{ filterCount(project, 'system') ?? '—' }}</span></button></details>
        <button v-else class="rc-category" :class="{ 'is-nested': 'nested' in item && item.nested }" :aria-pressed="category === item.key" @click="emit('category', item.key)"><span>{{ item.label }}</span><span v-if="project">{{ (materials ? materialCount(project, item.key) : filterCount(project, item.key)) ?? '—' }}</span></button>
      </template>
      <div class="rc-directory-note">{{ materials ? '制作资料只用于检查，不提供下载或打包。' : '只展示成功制作且可交付的成品。' }}<br />编辑请进入对应制作页面。</div>
    </nav>
    <section class="rc-file-workspace" aria-label="文件浏览">
      <div class="rc-mobile-category"><select class="rc-select" :value="category" aria-label="资源分类" @change="emit('category', ($event.target as HTMLSelectElement).value)"><option v-for="item in filters" :key="item.key" :value="item.key">{{ item.label }}</option></select></div>
      <div class="rc-toolbar rc-file-toolbar">
        <label class="rc-search"><Search :size="16" /><input :value="search" placeholder="搜索文件名称或相对路径" aria-label="搜索文件名称或相对路径" @input="emit('search', ($event.target as HTMLInputElement).value)" /><button v-if="search" class="rc-icon-button" aria-label="清除文件搜索" @click="emit('search', '')"><X :size="14" /></button></label>
        <select v-if="!projectId" class="rc-select" aria-label="筛选项目" value="" @change="emit('query', { project: ($event.target as HTMLSelectElement).value, page: undefined })"><option value="">全部项目</option><option v-for="item in projects" :key="item.project_id" :value="item.project_id">{{ item.name }}</option></select>
        <select class="rc-select" :value="extension" aria-label="文件类型" @change="emit('query', { extension: ($event.target as HTMLSelectElement).value, page: undefined })"><option value="">全部格式</option><option v-if="materials" value="txt">TXT 文本</option><option v-if="materials" value="json">JSON</option><option value="mp3">MP3 音频</option><option value="wav">WAV 音频</option><option value="flac">FLAC 音频</option><option value="m4a">M4A 音频</option><option value="ogg">OGG 音频</option><option v-if="!materials" value="zip">ZIP 压缩包</option><option v-if="materials" value="png">PNG 图片</option></select>
        <select class="rc-select" :value="sort" aria-label="文件排序" @change="emit('query', { sort: ($event.target as HTMLSelectElement).value, page: undefined })"><option value="name">名称顺序</option><option value="modified">最近修改</option><option value="size">文件大小</option></select>
      </div>
      <div v-if="path" class="rc-path-breadcrumb"><FolderOpen :size="14" /><button @click="emit('query', { path: undefined, mode: undefined, page: undefined })">{{ scopeLabel }}</button><template v-for="part in breadcrumbs" :key="part.path"><ChevronRight :size="12" /><button @click="emit('query', { path: part.path, mode: 'directory', q: undefined, page: undefined })">{{ part.name }}</button></template></div>
      <div class="rc-section-caption"><span>{{ scopeLabel }}<span class="rc-caption-divider">·</span>{{ directoryMode && !search ? '当前目录' : '含子目录' }}<span class="rc-caption-divider">·</span>{{ entries?.total ?? '…' }}项<span v-if="entries && !entries.complete" class="rc-warning-text">（已读取部分）</span></span><div class="rc-caption-actions"><button v-if="hasDirectory && !directoryMode" class="rc-text-link" @click="emit('browse-directory')"><FolderOpen :size="13" />浏览目录</button><button v-if="!materials" class="rc-text-link" :disabled="!entries?.total || !entries.complete || loading" @click="emit('export', true)"><Package :size="14" />批量下载成品</button></div></div>
      <div v-if="selection.size" class="rc-selection-toolbar" role="status"><span>已选择本页 <strong>{{ selection.size }}</strong> 个文件</span><div><button class="rc-button rc-button--small rc-button--primary" @click="emit('export', false)"><Package :size="14" />打包所选</button><button class="rc-button rc-button--quiet rc-button--small" @click="emit('clear-selection')">取消选择</button></div></div>
      <div v-if="error" class="rc-alert" role="alert"><span>{{ error }}</span><button class="rc-text-link" @click="emit('retry')">重试</button></div>
      <div v-if="loading && !entries" class="rc-file-table rc-skeleton" aria-label="正在读取文件清单"><div v-for="index in 8" :key="index" class="rc-skeleton-row"><span /><span /></div></div>
      <div v-else-if="entries?.items.length" class="rc-file-table" :class="{ 'is-refreshing': loading }" :aria-busy="loading">
        <table><thead><tr><th v-if="!materials" class="rc-check-column"><input type="checkbox" :checked="allSelected" aria-label="选择本页全部文件" @change="emit('select-page')" /></th><th>{{ materials ? '制作资料 / 相对路径' : '成品名称 / 类型' }}</th><th v-if="!projectId" class="rc-file-project-column">所属项目</th><th class="rc-size-column">大小</th><th class="rc-date-column">{{ materials ? '修改时间' : '完成时间' }}</th><th class="rc-file-actions-column">操作</th></tr></thead>
          <tbody><tr v-for="entry in entries.items" :key="entry.id" :class="{ 'is-selected': selection.has(entry.id), 'is-previewing': previewId === entry.id }"><td v-if="!materials" class="rc-check-column"><input v-if="entry.can_package" type="checkbox" :checked="selection.has(entry.id)" :aria-label="`选择 ${entry.name}`" @change="emit('select', entry.id)" /></td><td class="rc-file-name-cell"><div class="rc-file-name"><span class="rc-file-icon" :class="{ 'is-audio': entry.preview_kind === 'audio' }"><component :is="fileIcon(entry)" :size="17" /></span><button :title="entry.name" @click="entry.kind === 'directory' ? emit('directory', entry) : emit('preview', entry)">{{ entry.name }}</button><span v-if="entry.kind === 'directory'" class="rc-directory-label">目录</span></div><div class="rc-file-relative-path" :title="entry.relative_path">{{ entry.delivery_label || entry.relative_path }}</div></td><td v-if="!projectId" class="rc-file-project-column">{{ entry.project_name }}</td><td class="rc-size-column">{{ entry.kind === 'directory' ? '—' : resourceBytes(entry.size_bytes) }}</td><td class="rc-date-column"><time>{{ resourceDate(entry.completed_at || entry.modified_at) }}</time></td><td class="rc-file-actions-column"><div v-if="entry.kind === 'file'" class="rc-file-actions"><button class="rc-text-link" @click="emit('preview', entry)">{{ entry.can_preview ? '预览' : '详情' }}</button><button v-if="entry.can_download && !materials" class="rc-icon-button" :aria-label="`下载 ${entry.name}`" @click="emit('download', entry)"><Download :size="16" /></button><button class="rc-icon-button rc-file-copy" :aria-label="`复制 ${entry.name} 的相对路径`" @click="emit('copy', entry)"><Copy :size="14" /></button></div><button v-else class="rc-text-link" @click="emit('directory', entry)">进入<ArrowUpRight :size="13" /></button></td></tr></tbody>
        </table>
      </div>
      <div v-else-if="!error" class="rc-empty rc-empty--files"><FileText :size="30" /><h2>{{ search || extension ? '没有匹配文件' : (materials ? '此范围暂无制作资料' : '还没有可下载成品') }}</h2><p>{{ search || extension ? '搜索覆盖当前范围及子目录，试试调整关键词或格式。' : (materials ? '制作资料产生后自动更新，可在对应工作台继续处理。' : '完成音频合并、混音或分集后，成品会自动出现在这里。') }}</p><button v-if="search || extension" class="rc-button" @click="emit('search', ''); emit('query', { extension: undefined, page: undefined })">清除筛选</button></div>
      <div v-if="entries?.total" class="rc-pagination"><span>第{{ (page - 1) * 50 + 1 }}–{{ Math.min(page * 50, entries.total) }}项，共{{ entries.total }}项</span><div><button class="rc-icon-button" :disabled="page <= 1 || loading" aria-label="上一页" @click="emit('page', page - 1)"><ChevronLeft :size="17" /></button><span>{{ page }} / {{ pageCount }}</span><button class="rc-icon-button" :disabled="page >= pageCount || loading" aria-label="下一页" @click="emit('page', page + 1)"><ChevronRight :size="17" /></button></div></div>
    </section>
  </div>
</template>
