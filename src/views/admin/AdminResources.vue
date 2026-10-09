<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { FolderKanban, HardDrive, Music4, ScanSearch, Trash2, Files } from 'lucide-vue-next'
import Pager from '@/views/textformat/Pager.vue'
import AdminView from '@/components/admin/AdminView.vue'
import AdminTable from '@/components/admin/AdminTable.vue'
import ChartCard from '@/components/admin/ChartCard.vue'
import KpiCard from '@/components/admin/KpiCard.vue'
import MeterBar from '@/components/admin/MeterBar.vue'
import AdminChart from '@/components/admin/charts/AdminChart.vue'
import { barOption, donutOption } from '@/components/admin/charts/options'
import type { ChartTokens } from '@/components/admin/charts/tokens'
import Button from '@/components/ui/Button.vue'
import { useToast } from '@/components/ui/toast'
import { showConfirm } from '@/components/ui/dialog'
import { useAdminLoader } from '@/composables/useAdminLoader'
import * as api from '@/api/admin'
import { bytes, compact, percent, plain } from '@/utils/adminFormat'

const { push: toast } = useToast()
const resources = ref<api.AdminResources | null>(null)
const page = ref(1)
/** Catalog totals by default; a full directory inventory is an explicit, slower action. */
const fullScan = ref(false)

const loader = useAdminLoader(async () => {
  const result = await api.getResources(!fullScan.value, page.value)
  return () => {
    resources.value = result
    if (result.pagination) page.value = Math.min(page.value, Math.max(1, Math.ceil(result.pagination.total / result.pagination.page_size)))
  }
}, { onActionError: message => toast({ title: '操作未完成', description: message, variant: 'destructive' }) })
watch(page, () => { void loader.load() })
async function scan() { fullScan.value = true; await loader.load() }
function backToCatalog() { fullScan.value = false; page.value = 1; void loader.load() }

const storage = computed(() => resources.value?.project_storage)
const categories = computed(() => [...(storage.value?.categories ?? [])].sort((a, b) => b.size_bytes - a.size_bytes))
const cleanup = computed(() => storage.value?.cleanup_candidates)
const diskRatio = computed(() => resources.value?.disk_total_bytes ? resources.value.disk_used_bytes / resources.value.disk_total_bytes * 100 : null)
const topUsers = computed(() => [...(resources.value?.users ?? [])].sort((a, b) => b.size_bytes - a.size_bytes).slice(0, 10))

const diskChart = computed(() => (t: ChartTokens) => {
  const data = resources.value
  const projectBytes = storage.value?.size_bytes ?? 0
  const otherUsed = Math.max(0, (data?.disk_used_bytes ?? 0) - projectBytes)
  return donutOption(t, {
    title: '磁盘已用', total: percent(diskRatio.value, 0), format: value => bytes(value),
    data: [
      { name: fullScan.value ? '项目文件' : '已登记文件', value: projectBytes, color: t.series[0] },
      { name: '其他占用', value: otherUsed, color: t.series[3] },
      { name: '可用空间', value: data?.disk_free_bytes ?? 0, color: t.sequential[1] },
    ],
  })
})
const categoryChart = computed(() => (t: ChartTokens) => barOption(t, {
  horizontal: true, labels: true, categories: categories.value.map(row => row.label), format: value => bytes(value),
  series: [{ name: '大小', color: t.series[0], data: categories.value.map(row => row.size_bytes) }],
}))
const userChart = computed(() => (t: ChartTokens) => barOption(t, {
  horizontal: true, labels: true, categories: topUsers.value.map(row => row.username), format: value => bytes(value),
  series: [{ name: fullScan.value ? '实际占用' : '已登记文件', color: t.series[2], data: topUsers.value.map(row => row.size_bytes) }],
}))

async function cleanupTemp() {
  if (!fullScan.value) { fullScan.value = true; await loader.loadData() }
  const candidates = cleanup.value
  if (!candidates?.count) { toast({ title: '没有可清理的过期临时文件' }); return }
  if (!await showConfirm(`仅清理不活跃工作空间内超过 ${candidates.older_than_days} 天的临时缓存文件，预计 ${candidates.count} 个、${bytes(candidates.size_bytes)}。此操作不可恢复，继续？`, { title: '清理临时缓存', destructive: true })) return
  const result = await api.cleanupStaleTemp()
  await loader.loadData()
  toast({ title: `已清理 ${result.deleted_count} 个文件 · ${bytes(result.deleted_bytes)}`, variant: 'success' })
}
</script>

<template>
  <AdminView title="资源与存储" description="磁盘、项目文件与公共音乐库占用；按需盘点并安全清理过期缓存。" :loader="loader">
    <template #actions>
      <Button v-if="!fullScan" variant="outline" size="sm" :disabled="loader.loading.value" @click="scan"><ScanSearch class="h-4 w-4" />完整盘点</Button>
      <Button v-else variant="ghost" size="sm" :disabled="loader.loading.value" @click="backToCatalog">返回登记统计</Button>
    </template>
    <template v-if="resources">
      <div class="kpi-grid">
        <KpiCard label="磁盘使用" :value="percent(diskRatio, 1)" :icon="HardDrive" :accent="0" :hint="`${bytes(resources.disk_free_bytes)} 可用 · 共 ${bytes(resources.disk_total_bytes)}`" :tone="(diskRatio ?? 0) > 90 ? 'danger' : (diskRatio ?? 0) > 75 ? 'warning' : 'default'" />
        <KpiCard :label="fullScan ? '项目目录实际占用' : '已登记项目文件'" :value="bytes(storage?.size_bytes)" :icon="Files" :accent="1" :hint="`${compact(storage?.file_count)} 个文件`" />
        <KpiCard label="项目总数" :value="plain(resources.projects)" :icon="FolderKanban" :accent="2" :hint="`${resources.pagination?.total ?? resources.users.length} 位用户`" />
        <KpiCard label="公共音乐库" :value="`${plain(resources.music_library.count)} 首`" :icon="Music4" :accent="4" :hint="resources.music_library.size_bytes == null ? '完整盘点后显示大小' : `${bytes(resources.music_library.size_bytes)} · ${resources.music_library.assigned_chapters ?? 0} 次章节指派`" />
      </div>
      <p class="admin-notice">{{ resources.scope }}</p>

      <div class="chart-grid">
        <ChartCard title="磁盘构成" :subtitle="resources.root_path" :span="4">
          <AdminChart :option="diskChart" :height="240" label="磁盘构成环形图" />
          <template #footer><MeterBar :value="resources.disk_used_bytes" :max="resources.disk_total_bytes" :label="`${bytes(resources.disk_used_bytes)} / ${bytes(resources.disk_total_bytes)}`" :warn=".75" :critical=".9" /></template>
        </ChartCard>
        <ChartCard title="文件分类" :subtitle="fullScan ? '项目目录按类别扫描' : '已登记文件按类型'" :span="8"
                   :columns="[{ key: 'label', label: '类别' }, { key: 'count', label: '文件数' }, { key: 'size_bytes', label: '大小', format: bytes }]" :rows="categories">
          <AdminChart v-if="categories.length" :option="categoryChart" :height="Math.max(200, categories.length * 34)" label="文件分类占用条形图" />
          <p v-else class="admin-empty">暂无文件分类数据</p>
        </ChartCard>
        <ChartCard title="用户占用 Top 10" :subtitle="fullScan ? '项目目录实际大小' : '已登记文件大小'" :span="6">
          <AdminChart v-if="topUsers.length" :option="userChart" :height="Math.max(200, topUsers.length * 32)" label="用户存储占用条形图" />
          <p v-else class="admin-empty">暂无用户资源</p>
        </ChartCard>
        <ChartCard title="用户明细" :subtitle="fullScan ? '完整盘点 · 按实际占用前 20 位' : '每页 20 位'" :span="6">
          <AdminTable table-class="resource-users-table">
            <thead><tr><th>用户</th><th>项目</th><th>文件数</th><th>{{ fullScan ? '实际占用' : '登记大小' }}</th><th>登记文件</th></tr></thead>
            <tbody><tr v-for="row in resources.users" :key="row.username"><td>{{ row.username }}</td><td>{{ row.project_count ?? '—' }}</td><td>{{ plain(row.file_count ?? 0) }}</td><td>{{ bytes(row.size_bytes) }}</td><td>{{ plain(row.registered_file_count ?? row.count ?? 0) }}<small v-if="row.registered_file_bytes != null">{{ bytes(row.registered_file_bytes) }}</small></td></tr></tbody>
          </AdminTable>
          <Pager v-if="resources.pagination" :page="page" :page-count="Math.max(1, Math.ceil(resources.pagination.total / 20))" :total="resources.pagination.total" :page-size="20" unit="位用户" @update:page="page = $event" />
        </ChartCard>
      </div>

      <section class="cleanup-card">
        <div class="cleanup-card__icon"><Trash2 class="h-5 w-5" /></div>
        <div class="cleanup-card__text">
          <strong>临时文件清理</strong>
          <p v-if="cleanup">{{ plain(cleanup.count) }} 个超过 {{ cleanup.older_than_days }} 天的临时文件 · {{ bytes(cleanup.size_bytes) }}</p>
          <p v-else>完整盘点后显示可清理的过期临时文件。</p>
          <small>仅清理不活跃工作空间中的普通临时文件；跳过特殊文件和正在运行任务的工作空间。</small>
        </div>
        <Button variant="outline" :disabled="(fullScan && !cleanup?.count) || loader.loading.value || loader.actionBusy.value" @click="loader.runAction(cleanupTemp)"><Trash2 class="h-4 w-4" />清理过期临时文件</Button>
      </section>
    </template>
  </AdminView>
</template>
