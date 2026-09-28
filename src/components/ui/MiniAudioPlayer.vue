<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Pause, Play } from 'lucide-vue-next'
import { cn } from '@/lib/utils'
import { useAudioBus, type AudioPlayer } from '@/composables/useAudioBus'

/**
 * Compact inline audio player: a play/pause button, a thin seekable progress bar and a
 * small time readout. It renders inside a table row so a character's preview can be
 * played in place (no "click → scroll to the bottom" round-trip). All instances share
 * the `useAudioBus` bus, so starting one pauses whichever was playing.
 *
 * The `<audio>` element is created lazily on the FIRST play. Bulk row lists (BGM/Merge)
 * render dozens–hundreds of these players, and eager `preload="metadata"` per row queued
 * one metadata request per row on the ~6 shared same-origin connections — starving
 * SSE/API traffic and piling up media pipelines for rows nobody previews. The time
 * readout shows 0:00 until first play; the fetch itself is unchanged, it just starts
 * when the user asks for it. A resource that 404s / fails to decode shows an explicit
 * red「加载失败，重试」state instead of a silent dead 0:00/0:00.
 */
const props = withDefaults(defineProps<{
  src: string
  /** 已知时长（秒，如后端 ffprobe）：未播放前先显示真实时长而不是 0:00；
   *  loadedmetadata 后以真实值为准。缺省 = 未知（显示 --:--）。 */
  knownDuration?: number | null
}>(), { knownDuration: null })

const { claim, release } = useAudioBus()

const audioEl = ref<HTMLAudioElement | null>(null)
const audioReady = ref(false)
const playing = ref(false)
const loadError = ref(false)
const current = ref(0)
const duration = ref(0)

const progress = computed(() => (duration.value > 0 ? current.value / duration.value : 0))
/** 展示用总时长：实测值优先，其次已知的 ffprobe 值；都没有 = 未知（显示 --:--，不冒充 0:00）。 */
const shownDuration = computed(() => {
  if (duration.value > 0) return duration.value
  return (props.knownDuration && Number.isFinite(props.knownDuration) ? props.knownDuration : 0)
})

function stop(): void {
  audioEl.value?.pause()
}
const self: AudioPlayer = { stop }

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
  claim(self)
  void el.play().catch(() => {
    release(self)
    // play() 拒绝多为 AbortError（被别的播放器抢占）/ NotAllowedError（自动播放策略），
    // 资源本身有问题才会带 mediaError —— 只有它才是「加载失败」。
    if (audioEl.value?.error) onError()
  })
}

function fmt(t: number): string {
  if (!Number.isFinite(t)) return '0:00'
  const s = Math.max(0, Math.floor(t))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
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
  current.value = 0
  release(self)
}
/** 资源加载/解码失败：给出可重试的失败态，而不是停留在 0:00/0:00 让用户以为播放器坏了。 */
function onError(): void {
  audioEl.value?.pause()
  playing.value = false
  loadError.value = true
  release(self)
}
function onTimeUpdate(): void {
  if (audioEl.value) current.value = audioEl.value.currentTime
}
function onMeta(): void {
  if (audioEl.value) duration.value = audioEl.value.duration || 0
  loadError.value = false
}

function seek(e: MouseEvent): void {
  const el = audioEl.value
  const bar = e.currentTarget as HTMLElement | null
  if (!el || !bar || !(duration.value > 0)) return
  const rect = bar.getBoundingClientRect()
  const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width))
  el.currentTime = frac * duration.value
  current.value = el.currentTime
}

// A regenerated character gets a fresh preview file; pause and reset the state to match.
watch(
  () => props.src,
  () => {
    audioEl.value?.pause()
    playing.value = false
    current.value = 0
    duration.value = 0
    loadError.value = false
    release(self)
  },
)

onBeforeUnmount(() => {
  stop()
  release(self)
})
</script>

<template>
  <div class="flex items-center gap-2">
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
    <button
      type="button"
      :class="cn(
        'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-input bg-white/70 shadow-sm transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring dark:bg-card/60',
        playing && 'border-primary/40 bg-primary/10 text-primary',
        loadError && 'border-destructive/40 bg-destructive/10 text-destructive',
      )"
      :title="playing ? '暂停' : loadError ? '音频加载失败，点击重试' : '播放'"
      @click="toggle"
    >
      <Pause v-if="playing" class="h-3.5 w-3.5" />
      <Play v-else class="h-3.5 w-3.5" />
    </button>
    <div
      class="relative h-1.5 w-20 shrink-0 cursor-pointer overflow-hidden rounded-full bg-muted"
      title="点击跳转"
      @click="seek"
    >
      <div
        class="absolute inset-y-0 left-0 rounded-full bg-primary transition-[width] duration-75"
        :style="{ width: `${progress * 100}%` }"
      />
    </div>
    <span class="w-20 shrink-0 whitespace-nowrap text-right text-[11px] tabular-nums text-muted-foreground">
      <template v-if="loadError"><span class="text-destructive">加载失败，重试</span></template>
      <template v-else>{{ fmt(current) }} / {{ shownDuration > 0 ? fmt(shownDuration) : '--:--' }}</template>
    </span>
  </div>
</template>
