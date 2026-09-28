<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Pause, Play } from 'lucide-vue-next'
import { useAudioBus, type AudioPlayer } from '@/composables/useAudioBus'

/**
 * 章节级音频（06 合并 / 08 混音）的状态徽标播放器：默认只是一个带播放图标的徽标
 * （宽度≈文字，不占额外空间）；开始播放后原地平滑撑开（进度条 + 时间），
 * 暂停 / 播完 / 被其他播放器抢占时平滑收回。撑开与收回走同一组 DOM 节点，
 * 用 grid-template-columns 0fr ↔ 1fr 过渡，避免 v-if 切换的瞬时跳动。
 *
 * 与页面里所有播放器共享 useAudioBus：先点行内播放钮或右栏试听会自动暂停本钮并收回。
 * `<audio>` 惰性创建（首次点击才发请求），失败态与 MiniAudioPlayer 同款（红色可重试）。
 */
const props = withDefaults(defineProps<{
  src: string
  label: string
  /** teal = 已合并（06），sky = 已混音（08）——与左栏章节状态文字同色系。 */
  tone?: 'teal' | 'sky'
  /** 已知时长（秒，如后端 ffprobe）：未播放前先显示真实时长而不是 0:00。 */
  knownDuration?: number | null
  /** 从停止态开始播放时先定位到该秒（选中句的近似起点）；暂停后恢复不打断位置。 */
  startAt?: number | null
}>(), { tone: 'teal', knownDuration: null, startAt: null })

const { claim, release } = useAudioBus()

const audioEl = ref<HTMLAudioElement | null>(null)
const audioReady = ref(false)
const playing = ref(false)
const loadError = ref(false)
const current = ref(0)
const duration = ref(0)
/** 撑开策略：首次开始播放即撑开，且只有「播放完」或切章（src 变化）才收回——
 *  暂停 / 被其他播放器抢占（同为 pause）不收，位置保留可直接续播。 */
const expanded = ref(false)
/** metadata 未就绪时暂存的 seek 目标（开始播放前先定位到选中句起点）。 */
const pendingStart = ref<number | null>(null)

const progress = computed(() => (duration.value > 0 ? current.value / duration.value : 0))
/** 展示用总时长：实测值优先，其次 ffprobe 值；都没有 = 未知（--:--）。 */
const shownDuration = computed(() => {
  if (duration.value > 0) return duration.value
  return (props.knownDuration && Number.isFinite(props.knownDuration) ? props.knownDuration : 0)
})

function fmt(t: number): string {
  if (!Number.isFinite(t)) return '0:00'
  const s = Math.max(0, Math.floor(t))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

const colorClass = computed(() => {
  if (loadError.value) return 'border-destructive/40 bg-destructive/10 text-destructive'
  return props.tone === 'teal'
    ? 'border-teal-500/40 bg-teal-500/10 text-teal-700 hover:bg-teal-500/20 dark:text-teal-400'
    : 'border-sky-500/40 bg-sky-500/10 text-sky-700 hover:bg-sky-500/20 dark:text-sky-400'
})

const self: AudioPlayer = { stop: () => audioEl.value?.pause() }

async function toggle(): Promise<void> {
  if (playing.value) {
    audioEl.value?.pause()
    return
  }
  if (!audioReady.value) {
    audioReady.value = true
    await nextTick()
  }
  const el = audioEl.value
  if (!el) return
  loadError.value = false
  // 从停止态开始：先定位到选中句的近似起点（metadata 未就绪则等 onMeta 再定位）。
  const target = props.startAt
  if (target != null && Number.isFinite(target) && target > 0) {
    if (el.readyState >= 1) {
      const cap = el.duration > 0 ? el.duration - 0.01 : target
      el.currentTime = Math.min(Math.max(0, target), cap)
    } else {
      pendingStart.value = target
    }
  }
  expanded.value = true
  claim(self)
  void el.play().catch(() => {
    release(self)
    // play() 拒绝多为 AbortError（被别的播放器抢占）/ NotAllowedError（自动播放策略），
    // 资源本身有问题才会带 mediaError —— 只有它才是「加载失败」。
    if (audioEl.value?.error) onError()
  })
}

function onError(): void {
  audioEl.value?.pause()
  playing.value = false
  loadError.value = true
  release(self)
}
function onPlay(): void {
  playing.value = true
}
function onPause(): void {
  if (!playing.value) return
  playing.value = false
  release(self)
}
function onEnded(): void {
  playing.value = false
  expanded.value = false
  current.value = 0
  release(self)
}
function onTimeUpdate(): void {
  if (audioEl.value) current.value = audioEl.value.currentTime
}
function onMeta(): void {
  if (audioEl.value) duration.value = audioEl.value.duration || 0
  loadError.value = false
  if (pendingStart.value != null && audioEl.value) {
    const el = audioEl.value
    const target = pendingStart.value
    pendingStart.value = null
    el.currentTime = el.duration > 0 ? Math.min(target, el.duration - 0.01) : target
  }
}
function seek(e: MouseEvent): void {
  e.stopPropagation()
  const el = audioEl.value
  const bar = e.currentTarget as HTMLElement | null
  if (!el || !bar || !(duration.value > 0)) return
  const rect = bar.getBoundingClientRect()
  const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width))
  el.currentTime = frac * duration.value
  current.value = el.currentTime
}

// 切章 / 保存后 src 变化：停下并复位。
watch(
  () => props.src,
  () => {
    audioEl.value?.pause()
    playing.value = false
    expanded.value = false
    pendingStart.value = null
    current.value = 0
    duration.value = 0
    loadError.value = false
    release(self)
  },
)

onBeforeUnmount(() => {
  audioEl.value?.pause()
  release(self)
})
</script>

<template>
  <div class="flex shrink-0 items-center">
    <audio
      v-if="audioReady"
      ref="audioEl"
      :src="src"
      preload="metadata"
      class="hidden"
      @play="onPlay"
      @pause="onPause"
      @ended="onEnded"
      @timeupdate="onTimeUpdate"
      @loadedmetadata="onMeta"
      @durationchange="onMeta"
      @error="onError"
    />
    <!-- 单一容器：徽标 → 播放器原地平滑撑开（播放中 grid 轨道 0fr → 1fr 展开进度条与时间） -->
    <div
      class="inline-flex h-6 cursor-pointer items-center overflow-hidden rounded-full border px-1.5 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      :class="colorClass"
      :title="playing ? '暂停' : loadError ? `${label}·加载失败，点击重试` : expanded ? '继续播放' : `播放${label}音频`"
      role="button"
      tabindex="0"
      :aria-label="playing ? `暂停${label}音频` : `播放${label}音频`"
      @click="toggle"
      @keydown.enter.prevent="toggle"
      @keydown.space.prevent="toggle"
    >
      <Pause v-if="playing" class="h-3.5 w-3.5 shrink-0 px-0.5" />
      <Play v-else class="h-3.5 w-3.5 shrink-0 px-0.5" />
      <span class="shrink-0 pl-1.5 text-[11px] font-semibold">{{ label }}</span>
      <span
        class="grid overflow-hidden transition-[grid-template-columns,opacity] duration-300 ease-out"
        :class="expanded ? 'grid-cols-[1fr] opacity-100' : 'grid-cols-[0fr] opacity-0'"
      >
        <span class="flex min-w-0 items-center gap-2 whitespace-nowrap pl-2">
          <span
            class="relative block h-1 w-24 shrink-0 cursor-pointer overflow-hidden rounded-full bg-muted"
            title="点击跳转"
            @click.stop="seek"
          >
            <span class="absolute inset-y-0 left-0 rounded-full bg-primary transition-[width] duration-75" :style="{ width: `${progress * 100}%` }" />
          </span>
          <span class="text-[11px] tabular-nums">
            {{ fmt(current) }} / {{ shownDuration > 0 ? fmt(shownDuration) : '--:--' }}
          </span>
        </span>
      </span>
    </div>
  </div>
</template>
