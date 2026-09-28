<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref } from 'vue'
import { Pause, Play } from 'lucide-vue-next'
import { useAudioBus, type AudioPlayer } from '@/composables/useAudioBus'

/**
 * 列表行内的最小播放入口：一个圆角播放/暂停钮，没有进度条与时间读数——行内的进度/时间
 * 细节统一交给正在播放的行或右侧完整播放器（MiniAudioPlayer）。与 MiniAudioPlayer 共享
 * 同一个 useAudioBus 总线：先点别的行会自动暂停本钮（按钮图标随之复位）。
 * `<audio>` 惰性创建（首次点击才发请求），失败态与 MiniAudioPlayer 同款（红色重试）。
 */
withDefaults(defineProps<{ src: string }>(), { src: '' })

const { claim, release } = useAudioBus()

const audioEl = ref<HTMLAudioElement | null>(null)
const audioReady = ref(false)
const playing = ref(false)
const loadError = ref(false)

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
  claim(self)
  void el.play().catch(() => {
    release(self)
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
  release(self)
}
function onMeta(): void {
  loadError.value = false
}

onBeforeUnmount(() => {
  audioEl.value?.pause()
  release(self)
})
</script>

<template>
  <div class="flex items-center gap-1.5">
    <audio
      v-if="audioReady && src"
      ref="audioEl"
      :src="src"
      preload="none"
      class="hidden"
      @play="onPlay"
      @pause="onPause"
      @ended="onEnded"
      @loadedmetadata="onMeta"
      @error="onError"
    />
    <button
      type="button"
      :class="[
        'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-full border transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        playing
          ? 'border-primary/40 bg-primary/10 text-primary'
          : loadError
            ? 'border-destructive/40 bg-destructive/10 text-destructive'
            : 'border-input bg-white/70 text-foreground shadow-sm hover:bg-accent hover:text-accent-foreground dark:bg-card/60',
      ]"
      :title="playing ? '暂停' : loadError ? '音频加载失败，点击重试' : '播放'"
      @click="toggle"
    >
      <Pause v-if="playing" class="h-3.5 w-3.5" />
      <Play v-else class="h-3.5 w-3.5" />
    </button>
  </div>
</template>
