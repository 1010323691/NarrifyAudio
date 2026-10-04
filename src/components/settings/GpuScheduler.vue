<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useAuthStore } from '@/stores/auth'
import * as api from '@/api/admin'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardContent from '@/components/ui/CardContent.vue'
import Input from '@/components/ui/Input.vue'
import Button from '@/components/ui/Button.vue'

const props = defineProps<{ mode: 'paths' | 'parameters' | 'status' }>()
const auth = useAuthStore()
const draft = ref<api.GpuSchedulerConfig | null>(null)
const status = ref<api.GpuSchedulerStatus | null>(null)
const error = ref('')
const refreshError = ref('')
const message = ref('')
const busy = ref(false)
let epoch = 0
let refreshSequence = 0
let timer: ReturnType<typeof setInterval> | null = null

type NumericKey = Exclude<keyof api.GpuSchedulerConfig, 'enabled' | 'shutdown_when_idle' | 'llm_start_script_path' | 'llm_stop_script_path'>
const fields: { key: NumericKey; label: string; min: number; max: number }[] = [
  { key: 'scheduler_interval', label: '轮询间隔（秒）', min: 0.1, max: 300 },
  { key: 'min_service_runtime', label: '有任务时最短停留（秒）', min: 0, max: 86400 },
  { key: 'switch_cooldown', label: '切换冷却（秒）', min: 0, max: 86400 },
  { key: 'queue_difference_threshold', label: '队列压力差阈值', min: 0.1, max: 100000 },
  { key: 'max_wait_time', label: '最大等待（秒）', min: 0.1, max: 86400 },
  { key: 'idle_shutdown_timeout', label: '空闲关闭等待（秒）', min: 0, max: 86400 },
  { key: 'startup_timeout', label: '模型启动超时（秒）', min: 0.1, max: 3600 },
  { key: 'service_stop_timeout', label: '服务停止超时（秒）', min: 0.1, max: 600 },
  { key: 'drain_timeout', label: '等待 GPU 调用结束（秒）', min: 0.1, max: 86400 },
  { key: 'health_check_interval', label: '健康检查间隔（秒）', min: 0.1, max: 60 },
  { key: 'gpu_release_wait', label: '进程退出后显存释放等待（秒）', min: 0, max: 60 },
  { key: 'startup_retry_count', label: '模型启动总尝试次数', min: 1, max: 10 },
]
const states: Record<string, string> = {
  IDLE: '空闲', LLM_ACTIVE: 'LLM 活跃', TTS_ACTIVE: 'TTS 活跃', ERROR: '异常，等待恢复',
  SWITCHING_TO_LLM: '切换到 LLM', SWITCHING_TO_TTS: '切换到 TTS', SWITCHING_TO_IDLE: '关闭服务',
}
const phases: Record<string, string> = {
  DRAINING: '等待当前 GPU 调用结束', STOPPING: '停止旧服务', RELEASE_WAIT: '等待显存释放',
  STARTING: '启动服务', HEALTH_CHECK: '检查模型是否就绪', MODEL_LOADING: 'TTS 加载模型及 warmup',
}
const reasons: Record<string, string> = {
  'scheduler disabled': '调度未启用', 'initial backlog': '开始处理积压任务',
  'maximum wait exceeded': '另一侧等待超时', 'minimum runtime / cooldown': '保持最短运行与冷却',
  'minimum service runtime': '当前侧有任务，保持最短停留', 'switch cooldown': '等待切换冷却',
  'current queue empty': '当前队列为空', 'pressure difference exceeds threshold': '另一侧队列压力达到切换阈值',
  'keep current service': '保持当前服务', 'no waiting tasks': '暂无等待任务', 'idle timeout': '空闲时间到期',
}

async function refresh(loadDraft = false) {
  const ticket = epoch
  const sequence = ++refreshSequence
  const userId = auth.user?.id
  try {
    const current = await api.getGpuSchedulerStatus()
    if (ticket !== epoch || auth.user?.id !== userId) return
    if (sequence === refreshSequence) status.value = current
    if (loadDraft && props.mode !== 'status') {
      const result = await api.getGpuSchedulerConfig()
      if (ticket !== epoch || auth.user?.id !== userId) return
      draft.value = result.config
    }
    refreshError.value = ''
  } catch (cause: any) {
    if (ticket === epoch && auth.user?.id === userId && sequence === refreshSequence) refreshError.value = cause?.message || '读取 GPU 调度状态失败'
  }
}

async function save() {
  if (!draft.value || busy.value) return
  const ticket = epoch
  busy.value = true
  error.value = ''
  message.value = ''
  const patch: Partial<api.GpuSchedulerConfig> = props.mode === 'paths'
    ? { llm_start_script_path: draft.value.llm_start_script_path, llm_stop_script_path: draft.value.llm_stop_script_path }
    : Object.fromEntries(Object.entries(draft.value).filter(([key]) => !key.endsWith('_script_path')))
  try {
    const result = await api.updateGpuSchedulerConfig(patch)
    if (ticket !== epoch) return
    draft.value = result.config
    message.value = '配置已保存，Worker 将安全应用变更。'
    await refresh()
  } catch (cause: any) {
    if (ticket === epoch) error.value = cause?.message || '保存失败'
  } finally {
    if (ticket === epoch) busy.value = false
  }
}

async function recover() {
  const ticket = epoch
  busy.value = true
  error.value = ''
  try {
    await api.recoverGpuScheduler()
    if (ticket !== epoch) return
    message.value = '恢复请求已提交，Worker 将先确认 GPU 调用和进程已结束。'
    await refresh()
  } catch (cause: any) {
    if (ticket === epoch) error.value = cause?.message || '提交恢复请求失败'
  } finally {
    if (ticket === epoch) busy.value = false
  }
}

watch(() => auth.user?.id, () => {
  epoch++
  draft.value = null
  status.value = null
  error.value = ''
  refreshError.value = ''
  message.value = ''
  busy.value = false
  if (auth.user?.role === 'admin') void refresh(true)
})
onMounted(() => {
  void refresh(true)
  timer = setInterval(() => { if (document.visibilityState === 'visible') void refresh() }, 5000)
})
onBeforeUnmount(() => { epoch++; if (timer) clearInterval(timer) })
</script>

<template>
  <Card>
    <CardHeader><CardTitle>{{ mode === 'paths' ? 'LLM 服务启动与停止脚本' : mode === 'parameters' ? '单 GPU 动态调度配置' : 'GPU 服务调度' }}</CardTitle></CardHeader>
    <CardContent class="space-y-4">
      <p v-if="error" role="alert" class="text-sm text-destructive">{{ error }}</p>
      <p v-if="refreshError" role="alert" class="text-sm text-destructive">{{ refreshError }}</p>
      <p v-if="message" role="status" class="text-sm">{{ message }}</p>
      <template v-if="mode === 'paths' && draft">
        <label class="block space-y-2"><span>LLM 启动脚本绝对路径</span><Input v-model="draft.llm_start_script_path" placeholder="Windows：.ps1 / .cmd / .bat；Linux：.sh" /></label>
        <label class="block space-y-2"><span>LLM 停止脚本绝对路径（可选）</span><Input v-model="draft.llm_stop_script_path" /></label>
        <p class="text-sm text-muted-foreground">启动脚本必须保持前台运行，LLM 留在其进程树中。Linux 使用 Bash 执行 .sh，建议用 exec 启动服务。未配置停止脚本时发送退出信号；退出失败将暂停调度。保存路径后，在 Worker / Queue 中启用调度。</p>
        <Button :disabled="busy" @click="save">保存服务脚本</Button>
      </template>
      <template v-if="mode === 'parameters' && draft">
        <label class="flex items-center gap-2"><input v-model="draft.enabled" type="checkbox" />启用单 GPU 动态调度</label>
        <label class="flex items-center gap-2"><input v-model="draft.shutdown_when_idle" type="checkbox" />空闲到期关闭 GPU 服务</label>
        <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <label v-for="field in fields" :key="field.key" class="space-y-2"><span class="text-sm">{{ field.label }}</span><Input v-model.number="draft[field.key]" type="number" :min="field.min" :max="field.max" :step="field.key === 'startup_retry_count' ? 1 : 0.1" /></label>
        </div>
        <p class="text-sm text-muted-foreground">当前侧有等待任务或活动调用时，至少停留配置时长（默认 300 秒）；等待超时不能提前切走。当前侧清空且另一侧排队时，立即切换，不受最短停留或冷却限制。切换仍须确认进程退出。TTS 按任务加载模型，单次仅一个模型子进程使用 GPU。</p>
        <Button :disabled="busy" @click="save">保存调度配置</Button>
      </template>
      <div v-if="status" class="space-y-3 text-sm">
        <p>调度：{{ status.enabled ? '已启用' : '未启用' }} · {{ states[status.state] || status.state }}</p>
        <p v-if="status.enabled && status.stale" role="alert" class="text-amber-600">Worker 心跳已过期，以下为最后记录的状态。</p>
        <template v-if="mode !== 'paths'">
          <p>当前 GPU 服务：{{ status.current || '空闲' }} · 运行 {{ Math.round(status.runtime) }} 秒</p>
          <div class="overflow-x-auto"><table class="w-full text-left"><thead><tr><th>服务</th><th>等待任务</th><th>GPU 调用</th><th>压力</th><th>最老等待</th></tr></thead><tbody>
            <tr v-for="side in (['llm', 'tts'] as const)" :key="side"><td>{{ side.toUpperCase() }}</td><td>{{ status[side].waiting }}</td><td>{{ status[side].running }}</td><td>{{ status[side].pressure }}</td><td>{{ Math.round(status[side].oldest_wait) }} 秒</td></tr>
          </tbody></table></div>
          <p>上次切换：{{ status.last_switch ? new Date(status.last_switch * 1000).toLocaleString() : '尚未切换' }}</p>
          <p>{{ phases[status.phase] || reasons[status.reason] || status.reason }}</p>
        </template>
        <p v-if="status.error" role="alert" class="text-destructive">{{ status.error }}</p>
        <Button v-if="status.state === 'ERROR'" variant="outline" :disabled="busy || status.stale" @click="recover">请求安全恢复</Button>
      </div>
    </CardContent>
  </Card>
</template>
