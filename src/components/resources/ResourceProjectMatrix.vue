<script setup lang="ts">
import { Folder, ArrowRight, ChevronDown, ArrowUpRight, Package, Trash2, RefreshCw, FileSearch, Search, FolderPlus } from 'lucide-vue-next'
import ResourceTaskBrief from './ResourceTaskBrief.vue'
import type { ResourceProject } from '@/api/resources'
import type { TaskSnapshot } from '@/types'
import { categoryCount, resourceBytes, resourceDate } from '@/utils/resources'

defineProps<{
  materials?: boolean
  projects: ResourceProject[]
  totalProjects: number
  currentId: string
  loading: boolean
  projectSearch: string
  projectSort: string
  tasksForProject: (id: string) => TaskSnapshot[]
  updatingProjects: Set<string>
}>()
const emit = defineEmits<{
  'update:projectSearch': [value: string]
  'update:projectSort': [value: 'recent' | 'size' | 'name']
  open: [id: string, category?: string]
  production: [id: string]
  refresh: [id: string]
  export: [project: ResourceProject]
  trash: [project: ResourceProject]
  global: []
}>()

function status(item: ResourceProject) {
  if (item.scan_error) return '读取失败'
  if (!item.snapshot) return '清单准备中'
  if (!item.snapshot.complete) return '读取不完整'
  if (item.scan_task_id) return '正在更新'
  return '清单完整'
}
function textCount(item: ResourceProject) { return categoryCount(item, '01_input', '02_split_text', '03_parsed_json') }
</script>

<template>
  <section class="rc-projects" aria-label="各项目资源">
    <div class="rc-toolbar">
      <label class="rc-search"><Search :size="16" /><input :value="projectSearch" placeholder="搜索项目名称" aria-label="搜索项目名称" @input="emit('update:projectSearch', ($event.target as HTMLInputElement).value)" /></label>
      <select class="rc-select" :value="projectSort" aria-label="项目排序" @change="emit('update:projectSort', ($event.target as HTMLSelectElement).value as 'recent' | 'size' | 'name')"><option value="recent">最近文件更新</option><option value="size">占用从大到小</option><option value="name">项目名称</option></select>
      <button class="rc-button rc-button--quiet" @click="emit('global')"><FileSearch :size="16" />跨项目查找</button>
    </div>
    <div class="rc-section-caption"><span>{{ projects.length }} / {{ totalProjects }}个项目<span class="rc-caption-divider">·</span>成品与制作资料分别管理</span><span>点击分类数量，直接查看对应文件</span></div>
    <div v-if="loading" class="rc-matrix rc-skeleton" aria-label="正在读取项目资源"><div v-for="index in 4" :key="index" class="rc-skeleton-row"><span /><span /><span /></div></div>
    <div v-else-if="projects.length" class="rc-matrix">
      <div class="rc-matrix-head"><span>项目</span><span>{{ materials ? '文本与解析' : '可下载成品' }}</span><span>{{ materials ? '角色资料' : '成品容量' }}</span><span>{{ materials ? '制作音频' : '制作情况' }}</span><span>项目占用</span><span class="rc-align-end">操作</span></div>
      <article v-for="item in projects" :key="item.project_id" class="rc-project-row" :class="{ 'has-warning': item.scan_error || (item.snapshot && !item.snapshot.complete) }">
        <div class="rc-project-identity">
          <div class="rc-project-icon"><Folder :size="19" /></div>
          <div class="rc-project-copy">
            <button class="rc-project-name" @click="emit('open', item.project_id, materials ? 'production' : 'deliverables')">{{ item.name }}</button>
            <span v-if="item.project_id === currentId" class="rc-current-label">当前制作项目</span>
            <div class="rc-project-status" :title="item.scan_error || item.snapshot?.errors.map(error => `${error.path}: ${error.message}`).join('；')" :class="{ 'is-warning': item.scan_error || (item.snapshot && !item.snapshot.complete) }"><RefreshCw v-if="updatingProjects.has(item.project_id) || item.scan_task_id" :size="11" class="rc-spin" /><span>{{ updatingProjects.has(item.project_id) ? '资源更新中' : status(item) }}</span></div>
            <ResourceTaskBrief :tasks="tasksForProject(item.project_id)" />
          </div>
        </div>
        <div v-if="materials" class="rc-resource-cell" data-label="文本与解析">
          <button class="rc-count-link" :disabled="!item.snapshot" @click="emit('open', item.project_id, 'text')">{{ item.snapshot ? textCount(item) : '—' }}<span v-if="item.snapshot">份</span></button>
          <p v-if="item.snapshot && textCount(item)">{{ categoryCount(item, '02_split_text') }}章节 · {{ categoryCount(item, '03_parsed_json') }}解析<span v-if="categoryCount(item, '01_input')"> · {{ categoryCount(item, '01_input') }}原文</span></p><p v-else>{{ item.snapshot ? '暂无文本文件' : '等待清单' }}</p>
        </div>
        <div v-if="materials" class="rc-resource-cell" data-label="角色资料"><button class="rc-count-link" :disabled="!item.snapshot" @click="emit('open', item.project_id, '04_voice_profiles')">{{ item.snapshot ? categoryCount(item, '04_voice_profiles') : '—' }}<span v-if="item.snapshot">份</span></button><p>配置与参考音频</p></div>
        <div v-if="materials" class="rc-resource-cell" data-label="制作音频"><button class="rc-count-link" :disabled="!item.snapshot" @click="emit('open', item.project_id, 'audio')">{{ item.snapshot ? item.production_audio_count : '—' }}<span v-if="item.snapshot">份</span></button><p v-if="item.production_audio_count">合成片段与待处理音频</p><p v-else>{{ item.snapshot ? '尚未生成音频' : '等待清单' }}</p></div>
        <template v-if="!materials"><div class="rc-resource-cell" data-label="可下载成品"><button class="rc-count-link" :disabled="!item.snapshot" @click="emit('open', item.project_id, 'deliverables')">{{ item.snapshot ? item.delivery_count : '—' }}<span v-if="item.snapshot">份</span></button><p>{{ item.delivery_count ? '旁白、混音或分集成品' : '完成制作后领取' }}</p></div><div class="rc-resource-cell" data-label="成品容量"><strong>{{ resourceBytes(item.snapshot ? item.delivery_bytes : null) }}</strong><p>可交付文件的实际容量</p></div><div class="rc-resource-cell" data-label="制作情况"><span>{{ tasksForProject(item.project_id).length ? '制作进行中' : item.delivery_count ? '成品已就绪' : '等待制作' }}</span><p><button class="rc-text-link" @click="emit('production', item.project_id)">继续制作<ArrowUpRight :size="12" /></button></p></div></template><div class="rc-project-capacity" data-label="项目占用"><strong>{{ item.snapshot && !item.snapshot.complete ? '≥ ' : '' }}{{ resourceBytes(item.snapshot?.size_bytes) }}</strong><time>{{ resourceDate(item.snapshot?.latest_modified_at) }}</time><span v-if="item.snapshot" class="rc-scan-time">扫描 {{ resourceDate(item.snapshot.scanned_at) }}</span></div>
        <div class="rc-project-actions">
          <button class="rc-button rc-button--small" @click="emit('open', item.project_id)">{{ materials ? '检查资料' : '查看成品' }}<ArrowRight :size="14" /></button>
          <details class="rc-menu"><summary aria-label="项目更多操作"><ChevronDown :size="15" /></summary><div class="rc-menu-panel"><button @click="emit('production', item.project_id)"><ArrowUpRight :size="14" />进入制作</button><button @click="emit('refresh', item.project_id)"><RefreshCw :size="14" />重新读取清单</button><button :disabled="!item.snapshot?.complete || !item.delivery_count" v-if="!materials" @click="emit('export', item)"><Package :size="14" />批量下载成品</button><button class="is-destructive" @click="emit('trash', item)"><Trash2 :size="14" />移入项目回收站</button></div></details>
        </div>
      </article>
    </div>
    <div v-else class="rc-empty"><FolderPlus :size="30" /><h2>{{ totalProjects ? '没有匹配的项目' : '还没有项目资源' }}</h2><p>{{ totalProjects ? '调整项目名称搜索，或清除搜索条件。' : '创建项目并导入原文后，这里会展示完整资源。' }}</p><RouterLink v-if="!totalProjects" class="rc-button rc-button--primary" to="/dashboard">前往项目页<ArrowUpRight :size="14" /></RouterLink></div>
    <div v-if="projects.length" class="rc-section-footnote"><span>配置、日志和缓存计入项目占用，可在存储管理查看。</span><span>文件数量不代表章节完成率。</span></div>
  </section>
</template>
