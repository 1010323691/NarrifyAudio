import { defineStore } from 'pinia'
import { ref } from 'vue'
/**
 * Keep the small amount of cross-page state that cannot be read directly from
 * the active project's files.
 *
 *   排版与分册 files  →  文本解析 input   (the merged page keeps its state page-local;
 *                                handoff is on disk — 02_split_text/, which the parse
 *                                page lists directly)
 *
 * The parsed script selection is the one remaining handoff.
 */
export const usePipelineStateStore = defineStore('pipelineState', () => {
  // Which parsed JSON (a file name in 03_parsed_json/) the 音频合成 page operates on:
  // it writes the FIRST selected file on every user interaction and re-seeds the
  // selection on mount. 角色配音 no longer participates — its workbench always
  // covers every parsed chapter ('__all__'), never leaking a file pick downstream.
  // Empty string → the backend falls back to the most recently written JSON.
  const activeScript = ref('')

  function setActiveScript(name: string) {
    activeScript.value = name
  }

  function reset() {
    activeScript.value = ''
  }

  return {
    activeScript,
    setActiveScript,
    reset,
  }
})
