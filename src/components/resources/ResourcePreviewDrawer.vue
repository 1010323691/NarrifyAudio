<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { X, Download, Copy, ArrowUpRight, File, LoaderCircle, Pin, PinOff, ZoomIn, ZoomOut } from 'lucide-vue-next'
import { getResourcePreview, resourceFileUrl, type ResourceEntry, type ResourcePreview } from '@/api/resources'
import { resourceBytes, resourceDate } from '@/utils/resources'
import ResourceStructuredValue from './ResourceStructuredValue.vue'
import { useAudioBus } from '@/composables/useAudioBus'

const props = defineProps<{ entry: ResourceEntry; canPin?: boolean; pinned?: boolean }>()
const emit = defineEmits<{ close: []; production: [id: string, module: string]; pin: []; download: [entry: ResourceEntry]; copy: [entry: ResourceEntry] }>()
const panel = ref<HTMLElement | null>(null)
const audio = ref<HTMLAudioElement | null>(null)
const preview = ref<ResourcePreview | null>(null)
const loading = ref(false)
const error = ref('')
const mediaError = ref('')
const speed = ref('1')
const imageScale = ref(1)
const { claim, release } = useAudioBus()
let abort: AbortController | null = null
let sequence = 0
const previousFocus = document.activeElement as HTMLElement | null
const player = { stop: () => audio.value?.pause() }
const structured = computed(() => { if (!preview.value?.content || props.entry.preview_kind !== 'json') return null; try { return JSON.parse(preview.value.content) } catch { return null } })
const source = computed(() => `${resourceFileUrl(props.entry.id, 'preview')}?v=${encodeURIComponent(props.entry.modified_at)}`)

async function loadPreview() {
  const request = ++sequence
  abort?.abort()
  abort = new AbortController()
  preview.value = null
  loading.value = false
  error.value = ''
  mediaError.value = ''
  imageScale.value = 1
  player.stop()
  release(player)
  if (!['text', 'json', 'config'].includes(props.entry.preview_kind || '')) return
  loading.value = true
  try {
    const result = await getResourcePreview(props.entry.id, { signal: abort.signal })
    if (request === sequence) preview.value = result
  } catch (cause: any) {
    if (request === sequence && cause?.name !== 'AbortError') error.value = cause?.message || '文件暂时无法预览'
  } finally { if (request === sequence) loading.value = false }
}
function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); emit('close'); return }
  if (event.key !== 'Tab' || props.pinned || !panel.value) return
  const focusable = [...panel.value.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled]), select, audio[controls], [tabindex="0"]')]
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.value)) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
function changeSpeed() { if (audio.value) audio.value.playbackRate = Number(speed.value) }
watch(() => props.entry.id, async () => {
  void loadPreview()
  await nextTick()
  panel.value?.querySelector<HTMLButtonElement>('[data-close-preview]')?.focus()
}, { immediate: true })
watch(() => props.pinned, async () => { await nextTick(); panel.value?.querySelector<HTMLButtonElement>('[data-close-preview]')?.focus() })
onBeforeUnmount(() => { sequence += 1; abort?.abort(); player.stop(); release(player); if (previousFocus?.isConnected) previousFocus.focus() })
</script>

<template>
  <Teleport to="body" :disabled="pinned">
    <div class="rc-preview-host" :class="{ 'is-pinned': pinned }" @keydown="onKeydown" @click.self="emit('close')">
      <aside ref="panel" class="rc-preview-panel" :role="pinned ? 'region' : 'dialog'" :aria-modal="pinned ? undefined : true" aria-labelledby="rc-preview-title" tabindex="-1">
        <div class="rc-preview-header"><div class="rc-preview-title"><span class="rc-eyebrow">{{ entry.can_download ? '成品试听与详情' : '制作资料检查' }}</span><h2 id="rc-preview-title">{{ entry.name }}</h2><p>{{ entry.project_name }}<span>·</span>{{ entry.module_label }}</p></div><div class="rc-preview-header-actions"><button v-if="canPin" class="rc-icon-button" :aria-label="pinned ? '取消固定预览' : '固定预览到右侧'" @click="emit('pin')"><PinOff v-if="pinned" :size="17" /><Pin v-else :size="17" /></button><button data-close-preview class="rc-icon-button" aria-label="关闭文件预览" @click="emit('close')"><X :size="20" /></button></div></div>
        <div class="rc-preview-scroll">
          <dl class="rc-file-details"><dt>所属项目</dt><dd>{{ entry.project_name }}</dd><dt>相对路径</dt><dd class="rc-detail-path">{{ entry.relative_path }}<button class="rc-icon-button" aria-label="复制相对路径" @click="emit('copy', entry)"><Copy :size="14" /></button></dd><dt>文件大小</dt><dd>{{ resourceBytes(entry.size_bytes) }}</dd><dt>{{ entry.can_download ? '完成时间' : '修改时间' }}</dt><dd>{{ resourceDate(entry.completed_at || entry.modified_at) }}</dd><template v-if="entry.can_download"><dt>成品类型</dt><dd>{{ entry.delivery_label }}</dd><dt>制作版本</dt><dd>{{ entry.delivery_version }}</dd></template></dl>
          <div v-if="loading" class="rc-preview-loading"><LoaderCircle :size="21" class="rc-spin" />正在读取预览…</div>
          <div v-else-if="error" class="rc-alert" role="alert"><span>{{ error }}</span><button class="rc-text-link" @click="loadPreview">重新读取</button></div>
          <div v-else-if="entry.preview_kind === 'audio'" class="rc-audio-preview"><div class="rc-audio-heading"><span>试听音频</span><label>倍速<select class="rc-select" v-model="speed" aria-label="音频播放倍速" @change="changeSpeed"><option value="0.75">0.75×</option><option value="1">1.0×</option><option value="1.25">1.25×</option><option value="1.5">1.5×</option><option value="2">2.0×</option></select></label></div><audio ref="audio" :key="entry.id" :src="source" controls controlslist="nodownload" preload="metadata" @play="claim(player)" @pause="release(player)" @ended="release(player)" @loadedmetadata="changeSpeed" @error="mediaError = '音频无法加载或浏览器不支持此格式，请前往对应工作台检查。'" /><p v-if="mediaError" class="rc-warning-text" role="alert">{{ mediaError }}</p><p v-else class="rc-preview-hint">时长与进度来自音频本身，可拖动进度并调整音量。</p></div>
          <div v-else-if="entry.preview_kind === 'image'" class="rc-image-preview"><div class="rc-image-toolbar"><button class="rc-icon-button" :disabled="imageScale <= 0.5" aria-label="缩小图片" @click="imageScale = Math.max(.5, imageScale - .25)"><ZoomOut :size="17" /></button><button class="rc-text-link" @click="imageScale = 1">适应区域</button><button class="rc-icon-button" :disabled="imageScale >= 3" aria-label="放大图片" @click="imageScale = Math.min(3, imageScale + .25)"><ZoomIn :size="17" /></button></div><div class="rc-image-canvas"><img :src="source" :alt="entry.name" :style="{ width: `${imageScale * 100}%`, maxWidth: imageScale > 1 ? 'none' : '100%' }" @error="mediaError = '图片暂时无法加载，请前往对应工作台检查。'" /></div><p v-if="mediaError" class="rc-warning-text">{{ mediaError }}</p></div>
          <template v-else-if="preview"><div v-if="preview.warning" class="rc-notice" role="status">{{ preview.warning }}</div><div class="rc-preview-text-heading"><span>{{ entry.preview_kind === 'config' ? '配置预览 · 凭据已遮蔽' : '只读预览' }}</span><span v-if="preview.encoding">{{ preview.encoding }}</span></div><ResourceStructuredValue v-if="structured !== null" :value="structured" /><pre v-else-if="preview.content !== null" class="rc-text-preview" :class="{ 'is-code': ['json', 'config'].includes(entry.preview_kind || '') }">{{ preview.content }}</pre><div v-else class="rc-preview-unavailable"><File :size="27" /><p>此资料无法安全预览，请前往对应工作台检查。</p></div></template>
          <div v-else class="rc-preview-unavailable"><File :size="30" /><h3>此格式暂不支持在线预览</h3><p>请前往对应工作台检查资料。</p></div>
          <p v-if="!entry.can_download" class="rc-preview-hint">制作资料仅供检查，不提供下载或打包。</p>
        </div>
        <footer class="rc-preview-footer"><button v-if="entry.can_download" class="rc-button rc-button--primary" @click="emit('download', entry)"><Download :size="16" />下载成品</button><button class="rc-button" @click="emit('production', entry.project_id, entry.module)">前往制作<ArrowUpRight :size="15" /></button></footer>
      </aside>
    </div>
  </Teleport>
</template>
