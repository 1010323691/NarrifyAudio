<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Pause, Play } from 'lucide-vue-next'
import { cn } from '@/lib/utils'
import { useAudioBus, type AudioPlayer } from '@/composables/useAudioBus'

/**
 * Compact inline audio player: a play/pause button, a thin seekable progress bar and a
 * small time readout. It renders inside a table row so a character's preview can be
 * played in place (no "click → scroll to the bottom" round-trip). All instances share
 * the `useAudioBus` bus, so starting one pauses whichever was playing.
 *
 * `preload="metadata"` — the duration readout only needs the file header (a few KB,
 * read from the Xing/LAME header libmp3lame writes at the front of the file), while
 * the full mp3 is fetched only when playback starts. Rows are used in bulk on the
 * merge/BGM pages, so `preload="auto"` used to queue one FULL-file download per row
 * on the ~6 shared same-origin connections — starving SSE/API traffic and delaying
 * `loadedmetadata` until the readout sat at 0:00 until a page refresh.
 */
const props = defineProps<{ src: string }>()

const { claim, release } = useAudioBus()

const audioEl = ref<HTMLAudioElement | null>(null)
const playing = ref(false)
const current = ref(0)
const duration = ref(0)

const progress = computed(() => (duration.value > 0 ? current.value / duration.value : 0))

function stop(): void {
  audioEl.value?.pause()
}
const self: AudioPlayer = { stop }

function toggle(): void {
  const el = audioEl.value
  if (!el) return
  if (playing.value) {
    el.pause()
  } else {
    claim(self)
    void el.play().catch(() => release(self))
  }
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
function onTimeUpdate(): void {
  if (audioEl.value) current.value = audioEl.value.currentTime
}
function onMeta(): void {
  if (audioEl.value) duration.value = audioEl.value.duration || 0
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
    />
    <button
      type="button"
      :class="cn(
        'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-input bg-background shadow-sm transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        playing && 'border-primary/40 bg-primary/10 text-primary',
      )"
      :title="playing ? '暂停' : '播放'"
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
      {{ fmt(current) }} / {{ fmt(duration) }}
    </span>
  </div>
</template>
