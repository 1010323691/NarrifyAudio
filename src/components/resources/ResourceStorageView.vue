<script setup lang="ts">
import { computed } from 'vue'
import { HardDrive, Trash2, Package, ArrowUpRight, Download, RefreshCw } from 'lucide-vue-next'
import type { ResourceOverview } from '@/api/resources'
import { resourceBytes, resourceDate } from '@/utils/resources'
import { resourceExportUrl } from '@/api/resources'
const props = defineProps<{ overview: ResourceOverview | null; loading: boolean }>()
defineEmits<{ cleanup: [ids: string[]]; refresh: [] }>()
const total = computed(() => props.overview ? props.overview.storage.project_bytes + props.overview.storage.trash_bytes + props.overview.storage.export_bytes : 0)
const categories = computed(() => {
  if (!props.overview) return []
  const source = props.overview.categories
  const group = (keys: string[]) => source.filter(item => keys.includes(item.key)).reduce((sum, item) => ({ count: sum.count + item.count, size_bytes: sum.size_bytes + item.size_bytes }), { count: 0, size_bytes: 0 })
  const cache = group(['00_temp', '.cache', 'cache'])
  const system = group(['config', 'logs'])
  const delivery = props.overview.projects.reduce((sum, item) => ({ count: sum.count + item.delivery_count, size_bytes: sum.size_bytes + item.delivery_bytes }), { count: 0, size_bytes: 0 })
  const totalCount = source.reduce((sum, item) => sum + item.count, 0)
  return [
    { key: 'deliverables', label: '可交付成品', ...delivery },
    { key: 'production', label: '制作资料', count: Math.max(0, totalCount - delivery.count - cache.count - system.count), size_bytes: Math.max(0, props.overview.storage.project_bytes - delivery.size_bytes - cache.size_bytes - system.size_bytes) },
    { key: 'system', label: '配置与运行日志', ...system },
    { key: 'cache', label: '临时缓存', ...cache },
  ].sort((a, b) => b.size_bytes - a.size_bytes)
})
const maximum = computed(() => Math.max(1, ...categories.value.map(item => item.size_bytes)))
</script>
<template>
  <section class="rc-storage" aria-label="存储管理">
    <div class="rc-storage-heading"><div><h2>存储去向</h2><p>容量来自已读取的清单，清理完成后自动重新核算。</p></div><button class="rc-button" :disabled="loading" @click="$emit('refresh')"><RefreshCw :size="15" />更新容量</button></div>
    <div class="rc-storage-cards">
      <article><HardDrive :size="20" /><span>活动项目</span><strong>{{ overview?.storage.project_complete === false ? '≥ ' : '' }}{{ resourceBytes(overview?.storage.project_bytes) }}</strong><p>包括业务文件、配置、日志和缓存</p></article>
      <article><Trash2 :size="20" /><span>项目回收站</span><strong>{{ overview?.storage.trash_complete === false ? '≥ ' : '' }}{{ resourceBytes(overview?.storage.trash_bytes) }}</strong><p>移入回收站不会立即释放空间</p><RouterLink to="/trash" class="rc-text-link">管理回收站<ArrowUpRight :size="13" /></RouterLink></article>
      <article><Package :size="20" /><span>导出包</span><strong>{{ resourceBytes(overview?.storage.export_bytes) }}</strong><p>独立副本，保留7天后自动清理</p></article>
    </div>
    <p class="rc-storage-total">已统计合计 <strong>{{ overview ? resourceBytes(total) : '未读取' }}</strong><span v-if="overview && (!overview.storage.project_complete || !overview.storage.trash_complete)" class="rc-warning-text"> · 部分目录尚未完整读取，合计为已读取容量</span></p>
    <div class="rc-storage-columns">
      <section class="rc-storage-breakdown"><h3>活动项目分类占用</h3><div v-for="item in categories" :key="item.key" class="rc-storage-category"><div><span>{{ item.label }}</span><span>{{ item.count }}项 · {{ resourceBytes(item.size_bytes) }}</span></div><div class="rc-capacity-bar"><span :style="{ width: `${item.size_bytes / maximum * 100}%` }" /></div></div><p v-if="!categories.length" class="rc-muted">{{ loading ? '正在读取容量…' : '暂无已读取的项目文件' }}</p></section>
      <section class="rc-cleanup-card"><div class="rc-cleanup-icon"><Trash2 :size="21" /></div><h3>清理过期临时缓存</h3><p>只处理指定缓存目录中超过7天的临时文件。章节原文、角色资料、音频成品和项目配置不在清理范围。</p><ul><li>先展示候选目录、文件和可释放容量</li><li>项目有活动任务时跳过该项目</li><li>执行时重新核对文件，已修改的文件跳过</li></ul><button class="rc-button" :disabled="!overview?.projects.length || loading" @click="$emit('cleanup', overview!.projects.map(item => item.project_id))">查看可清理明细<ArrowUpRight :size="14" /></button></section>
    </div>
    <section class="rc-exports"><div class="rc-storage-heading"><div><h3>成品合集与历史导出</h3><p>打包进度显示在任务栏，完成后下载入口自动出现。</p></div></div><div v-if="overview?.exports.length" class="rc-export-list"><article v-for="item in overview.exports" :key="item.task_id"><div class="rc-export-icon"><Package :size="20" /></div><div class="rc-export-copy"><strong>{{ item.name }}</strong><p>{{ item.file_count }}个文件 · {{ resourceBytes(item.size_bytes) }} · {{ item.available ? '保留至' : item.unavailable_reason || '已到期或不可用' }} {{ resourceDate(item.expires_at) }}</p></div><a v-if="item.available" class="rc-button rc-button--small" :href="resourceExportUrl(item.task_id)"><Download :size="14" />下载ZIP</a><span v-else class="rc-muted">不可下载</span></article></div><div v-else class="rc-compact-empty"><Package :size="22" /><span>还没有成品合集。到成品页选择音频，批量下载。</span></div></section>
  </section>
</template>
