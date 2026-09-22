import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type {
  AudioCutResult,
  BatchResult,
  MergeResult,
  PrepareFoundationsResult,
} from '@/types'

/**
 * Pipeline handoff (requirement #3): each stage records the result it produced
 * so the dashboard can show per-stage completion and offer "前往下一步".
 *
 *   排版与分册 files  →  文本解析 input   (the merged page keeps its state page-local;
 *                                handoff is on disk — 02_split_text/, which the parse
 *                                page lists directly)
 *   音频合并 output   →  音频分集 input
 *
 * The middle stages (文本解析 / 角色配音 / 音频合成 / 音频合并) all read fixed
 * workspace files (03_parsed_json / 04_voice_profiles / 05_audio_chunk), so they need no
 * path handoff — only a "done" marker for the dashboard.
 */
export const useProjectStore = defineStore('project', () => {
  // Which parsed JSON (a file name in 03_parsed_json/) the downstream 角色配音 / 音频合成
  // stages should read. Shared by both pages so they operate on the same file.
  // Empty string → the backend falls back to the most recently written JSON.
  const activeScript = ref('')
  const voiceResult = ref<PrepareFoundationsResult | null>(null)
  const batchResult = ref<BatchResult | null>(null)
  const mergeResult = ref<MergeResult | null>(null)

  const audioOutputs = ref<string[]>([]) // cut .mp3 paths
  const audioResult = ref<AudioCutResult | null>(null)

  // The audio stage consumes *audio* — the merged audiobook (or a user-picked file).
  const audioInput = computed(() => mergeResult.value?.path ?? null)

  function setActiveScript(name: string) {
    activeScript.value = name
  }
  function recordVoices(r: PrepareFoundationsResult) {
    voiceResult.value = r
  }
  function recordBatch(r: BatchResult) {
    batchResult.value = r
  }
  function recordMerge(r: MergeResult) {
    mergeResult.value = r
  }
  function recordAudio(r: AudioCutResult) {
    audioResult.value = r
    audioOutputs.value = r.files.map((f) => f.path)
  }

  return {
    activeScript,
    voiceResult,
    batchResult,
    mergeResult,
    audioOutputs,
    audioResult,
    audioInput,
    setActiveScript,
    recordVoices,
    recordBatch,
    recordMerge,
    recordAudio,
  }
})
