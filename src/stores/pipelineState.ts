import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type {
  MergeResult,
} from '@/types'

/**
 * Keep the small amount of cross-page state that cannot be read directly from
 * the active project's files.
 *
 *   排版与分册 files  →  文本解析 input   (the merged page keeps its state page-local;
 *                                handoff is on disk — 02_split_text/, which the parse
 *                                page lists directly)
 *   音频合并 output   →  音频分集 input
 *
 * Parsed script selection and the merge result are the two current handoffs.
 */
export const usePipelineStateStore = defineStore('pipelineState', () => {
  // Which parsed JSON (a file name in 03_parsed_json/) the downstream 角色配音 / 音频合成
  // stages should read. Shared by both pages so they operate on the same file.
  // Empty string → the backend falls back to the most recently written JSON.
  const activeScript = ref('')
  const mergeResult = ref<MergeResult | null>(null)

  // The audio stage consumes *audio* — the merged audiobook (or a user-picked file).
  const audioInput = computed(() => mergeResult.value?.path ?? null)

  function setActiveScript(name: string) {
    activeScript.value = name
  }
  function recordMerge(r: MergeResult) {
    mergeResult.value = r
  }

  function reset() {
    activeScript.value = ''
    mergeResult.value = null
  }

  return {
    activeScript,
    mergeResult,
    audioInput,
    setActiveScript,
    recordMerge,
    reset,
  }
})
