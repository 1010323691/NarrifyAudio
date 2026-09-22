import { ref } from 'vue'

/**
 * A module-scoped "only one preview plays at a time" bus for the compact per-row
 * players on the 角色配音 page. Each MiniAudioPlayer claims the bus when it starts
 * playing — pausing whatever was active — and releases it on pause / end / unmount.
 * The `active` ref lives at module scope so every player instance shares one bus.
 */
export interface AudioPlayer {
  /** Pause this player (the bus calls it when another player takes over). */
  stop: () => void
}

const active = ref<AudioPlayer | null>(null)

export function useAudioBus() {
  function claim(self: AudioPlayer): void {
    const prev = active.value
    if (prev && prev !== self) prev.stop()
    active.value = self
  }
  function release(self: AudioPlayer): void {
    if (active.value === self) active.value = null
  }
  return { claim, release }
}
