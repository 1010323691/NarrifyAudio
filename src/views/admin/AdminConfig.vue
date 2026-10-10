<script setup lang="ts">
import { ref } from 'vue'
import AdminView from '@/components/admin/AdminView.vue'
import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardContent from '@/components/ui/CardContent.vue'
import Input from '@/components/ui/Input.vue'
import Switch from '@/components/ui/Switch.vue'
import { useToast } from '@/components/ui/toast'
import { showConfirm } from '@/components/ui/dialog'
import AdminSettings from '@/components/settings/AdminSettings.vue'
import GpuScheduler from '@/components/settings/GpuScheduler.vue'
import { useClientDisplayStore } from '@/stores/clientDisplay'
import { errorMessage, useAdminLoader } from '@/composables/useAdminLoader'
import * as api from '@/api/admin'
import { bytes } from '@/utils/adminFormat'

type SettingsSection = 'text' | 'models' | 'audio' | 'general' | 'storage' | 'runtime'
const SECTIONS: [SettingsSection, string][] = [['text', '文本处理'], ['models', '解析与 LLM'], ['audio', 'TTS 与音频'], ['general', '账户与客户端'], ['storage', '存储路径'], ['runtime', 'Worker / 调度']]
const clientDisplay = useClientDisplayStore()
const { push: toast } = useToast()
const settingsSection = ref<SettingsSection>('text')
const storage = ref<api.StorageSettings | null>(null)
const quota = ref<api.QuotaSettings | null>(null)
const registration = ref<api.RegistrationSettings | null>(null)
const runtime = ref<api.RuntimeSettings | null>(null)
const rootDraft = ref('')
const quotaDraft = ref('0')
const savingToggle = ref<'logs' | 'registration' | null>(null)
const savingQuota = ref(false)
const retention = ref<api.ProjectRetentionSettings | null>(null)
const ttlDraft = ref('30')
const trashDraft = ref('7')
const savingRetention = ref(false)

const loader = useAdminLoader(async () => {
  await clientDisplay.load()
  const [s, q, r, limits, keep] = await Promise.all([
    api.getStorageSettings(), api.getQuotaSettings(), api.getRegistrationSettings(), api.getRuntimeSettings().catch(() => null),
    api.getProjectRetentionSettings().catch(() => null),
  ])
  return () => {
    storage.value = s; quota.value = q; registration.value = r; runtime.value = limits
    retention.value = keep
    if (keep) { ttlDraft.value = String(keep.project_ttl_days); trashDraft.value = String(keep.trash_days) }
    rootDraft.value = s.root_path
    quotaDraft.value = String(q.initial_units)
  }
}, { onActionError: message => toast({ title: '操作未完成', description: message, variant: 'destructive' }) })
const { runAction, actionBusy, loading, error } = loader

async function saveToggle(kind: 'logs' | 'registration') {
  if (actionBusy.value) return
  savingToggle.value = kind
  try { await runAction(kind === 'logs' ? toggleClientLogs : toggleRegistration) }
  finally { savingToggle.value = null }
}
async function submitQuota() {
  if (actionBusy.value) return
  savingQuota.value = true
  try { await runAction(saveQuota) }
  finally { savingQuota.value = false }
}
async function toggleClientLogs() {
  try {
    if (await clientDisplay.save(!clientDisplay.logsEnabled)) toast({ title: '客户端日志显示设置已保存', variant: 'success' })
  } catch (cause) { error.value = errorMessage(cause) }
}
async function toggleRegistration() {
  if (!registration.value) return
  try { registration.value = await api.updateRegistrationSettings(!registration.value.enabled); toast({ title: '注册设置已保存', variant: 'success' }) }
  catch (cause) { error.value = errorMessage(cause) }
}
async function submitRetention() {
  if (actionBusy.value) return
  savingRetention.value = true
  try { await runAction(saveRetention) }
  finally { savingRetention.value = false }
}
async function saveRetention() {
  const ttl = Number(ttlDraft.value), trash = Number(trashDraft.value)
  if (!Number.isInteger(ttl) || ttl < 0 || ttl > 3650 || !Number.isInteger(trash) || trash < 1 || trash > 3650) {
    error.value = '项目保质期为 0～3650 天的整数（0 表示不过期），回收站保留为 1～3650 天的整数'
    return
  }
  if (ttl > 0 && !window.confirm(`确认保存？创建已超过 ${ttl} 天的现有项目会在下一轮清理（约 1 分钟内）被永久删除，包括音频、文件、日志和数据库记录，且不可恢复。`)) return
  try { retention.value = await api.updateProjectRetentionSettings({ project_ttl_days: ttl, trash_days: trash }); toast({ title: '项目保留期限已保存', variant: 'success' }) }
  catch (cause) { error.value = errorMessage(cause) }
}
async function saveQuota() {
  const value = Number(quotaDraft.value)
  if (!Number.isInteger(value) || value < 0) { error.value = '初始额度必须是非负整数'; return }
  try { quota.value = await api.updateQuotaSettings(value); toast({ title: '初始额度已保存', variant: 'success' }) }
  catch (cause) { error.value = errorMessage(cause) }
}
async function saveRoot() {
  if (!await showConfirm('修改存储根目录会迁移已登记工作空间文件。确认继续？', { title: '修改存储根目录', destructive: true })) return
  try { storage.value = await api.updateStorageRoot(rootDraft.value); toast({ title: '存储根目录已保存', variant: 'success' }) }
  catch (cause) { error.value = errorMessage(cause) }
}
</script>

<template>
  <AdminView title="系统配置" description="统一管理平台制作参数、账户策略、存储与运行设置。" :loader="loader">
    <div class="admin-config">
      <nav class="settings-nav" aria-label="系统配置分类">
        <button v-for="item in SECTIONS" :key="item[0]" type="button" :class="settingsSection === item[0] ? 'active' : ''" :aria-pressed="settingsSection === item[0]" @click="settingsSection = item[0]">{{ item[1] }}</button>
      </nav>
      <AdminSettings v-if="settingsSection === 'text' || settingsSection === 'models' || settingsSection === 'audio'" :section="settingsSection" />
      <GpuScheduler v-if="settingsSection === 'models'" mode="paths" />
      <GpuScheduler v-if="settingsSection === 'runtime'" mode="parameters" />
      <Card v-if="settingsSection === 'general'"><CardHeader><CardTitle>账户与客户端</CardTitle></CardHeader><CardContent>
        <div class="admin-setting-row">
          <div><h3>客户端功能日志</h3><p>统一控制实时日志和模型输出，已打开的客户端会自动同步。任务进度与失败提示继续显示。</p></div>
          <div class="admin-toggle-control"><span class="admin-toggle-feedback">即时保存</span><Switch aria-label="客户端功能日志" :model-value="clientDisplay.logsEnabled" :disabled="!clientDisplay.loaded || actionBusy" :busy="savingToggle === 'logs'" @update:model-value="saveToggle('logs')" /></div>
        </div>
        <div class="admin-setting-row">
          <div><h3>新用户注册</h3><p>控制登录页面是否允许新用户自行创建账户。关闭后可在「用户管理」中由管理员直接开户。</p></div>
          <div class="admin-toggle-control"><span class="admin-toggle-feedback">即时保存</span><Switch aria-label="允许新用户注册" :model-value="registration?.enabled ?? false" :disabled="!registration || loading || actionBusy" :busy="savingToggle === 'registration'" @update:model-value="saveToggle('registration')" /></div>
        </div>
        <div class="admin-setting-row">
          <div><label for="initial-quota">新用户初始额度</label><p>设置新账户获得的制作额度，必须为非负整数。</p></div>
          <div class="controls"><Input id="initial-quota" v-model="quotaDraft" type="number" min="0" class="w-28" /><Button variant="outline" size="sm" class="admin-quota-save" :disabled="!quota || actionBusy" @click="submitQuota">{{ savingQuota ? '保存中…' : '保存' }}</Button></div>
        </div>
        <div class="admin-setting-row">
          <div><label for="project-ttl">项目自然保质期（天）</label><p>项目创建满该天数后整体永久删除：工作空间文件、音频、日志、资源导出与全部数据库记录。默认 30，0 表示不过期；「默认工作空间」不受影响；仍有未完成任务的项目也会被删除（先取消任务）。</p></div>
          <div class="controls"><Input id="project-ttl" v-model="ttlDraft" type="number" min="0" max="3650" class="w-28" /></div>
        </div>
        <div class="admin-setting-row">
          <div><label for="trash-days">回收站保留（天）</label><p>项目移入回收站后可恢复的天数，到期自动永久删除，默认 7。</p></div>
          <div class="controls"><Input id="trash-days" v-model="trashDraft" type="number" min="1" max="3650" class="w-28" /><Button variant="outline" size="sm" :disabled="!retention || actionBusy" @click="submitRetention">{{ savingRetention ? '保存中…' : '保存' }}</Button></div>
        </div>
      </CardContent></Card>
      <Card v-else-if="settingsSection === 'storage'"><CardHeader><CardTitle>存储路径</CardTitle></CardHeader><CardContent class="admin-form">
        <label for="storage-root">工作空间根目录</label><Input id="storage-root" v-model="rootDraft" class="mono" />
        <p class="admin-muted">当前路径 {{ storage?.root_path || '读取中' }} · 来源：{{ storage?.source === 'admin' ? '管理员配置' : '部署默认值' }}</p>
        <p class="admin-muted">更改时后端会迁移已登记的工作空间目录。</p><Button :disabled="!storage || actionBusy" @click="runAction(saveRoot)">{{ actionBusy ? '保存中…' : '保存存储根目录' }}</Button>
      </CardContent></Card>
      <Card v-else-if="settingsSection === 'runtime'"><CardHeader><CardTitle>部署运行限制 · 只读</CardTitle></CardHeader><CardContent class="kv-list">
        <template v-if="runtime"><p><span>任务租约</span><strong>{{ runtime.limits.task_lease_seconds }} 秒</strong></p><p><span>最大重试次数</span><strong>{{ runtime.limits.task_max_attempts }}</strong></p><p><span>上传大小上限</span><strong>{{ bytes(runtime.limits.max_upload_bytes) }}</strong></p><p><span>会话时长</span><strong>{{ runtime.limits.session_ttl_hours }} 小时</strong></p><p><span>配置来源</span><strong>部署环境变量</strong></p></template>
        <p v-else class="admin-muted">此运行环境尚未提供非敏感运行限制数据。</p>
      </CardContent></Card>
    </div>
  </AdminView>
</template>
