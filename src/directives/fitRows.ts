import type { Directive } from 'vue'

/**
 * 固定十行盒子的「按视口定行高」：行高只由视口/布局决定，与行内容无关。
 *
 * 盒子高度 = 表头 + 10 × 行高（由 CSS 变量 `prop` 驱动）。先把行高放到最大值（CSS 默认值），
 * 若盒子的任一滚动/裁切祖先因此溢出（母容器出现滚动条、分页条被挤出视口），就把溢出量平摊到
 * 10 行上缩小行高，直到刚好装下；视口足够高时保持默认行高。20/50 条仍在盒内滚动，盒高不变。
 */
export interface FitRowsOptions {
  /** 驱动盒高的 CSS 变量，如 `--list-row-height` */
  prop: string
  /** 行高上限（缺省取 CSS 里该变量的声明值） */
  max?: number
  /** 行高下限：再矮就让母容器滚动，保证行内容不被压坏 */
  min?: number
  rows?: number
}

const STEP = 64

function clippingAncestors(el: HTMLElement): HTMLElement[] {
  const out: HTMLElement[] = []
  for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
    const overflow = getComputedStyle(node).overflowY
    if (overflow !== 'visible') out.push(node)
  }
  return out
}

function maxOverflow(ancestors: HTMLElement[]): number {
  let worst = 0
  for (const node of ancestors) worst = Math.max(worst, node.scrollHeight - node.clientHeight)
  return worst
}

function declaredMax(el: HTMLElement, prop: string, fallback: number): number {
  el.style.removeProperty(prop)
  const value = parseFloat(getComputedStyle(el).getPropertyValue(prop))
  return Number.isFinite(value) && value > 0 ? value : fallback
}

function fit(el: HTMLElement, options: FitRowsOptions) {
  if (!el.isConnected || !el.offsetParent) return
  const rows = options.rows ?? 10
  const min = options.min ?? 32
  const max = options.max ?? declaredMax(el, options.prop, 40)
  const ancestors = clippingAncestors(el)
  let height = max
  el.style.setProperty(options.prop, `${height}px`)
  for (let i = 0; i < 3; i++) {
    const overflow = maxOverflow(ancestors)
    if (overflow <= 0.5 || height <= min) break
    height = Math.max(min, Math.floor((height - overflow / rows) * STEP) / STEP)
    el.style.setProperty(options.prop, `${height}px`)
  }
}

interface Bound { frame: number; resize: ResizeObserver; onWindow: () => void; opts: FitRowsOptions }
const bound = new WeakMap<HTMLElement, Bound>()

function schedule(el: HTMLElement) {
  const state = bound.get(el)
  if (!state || state.frame) return
  // setTimeout 而非 rAF：后台/未绘制的标签页 rAF 不触发，行高会停在未适配状态。
  state.frame = window.setTimeout(() => {
    state.frame = 0
    fit(el, state.opts)
  }, 30)
}

export const vFitRows: Directive<HTMLElement, FitRowsOptions> = {
  mounted(el, binding) {
    const resize = new ResizeObserver(() => schedule(el))
    const onWindow = () => schedule(el)
    bound.set(el, { frame: 0, resize, onWindow, opts: binding.value })
    for (const node of clippingAncestors(el).slice(0, 2)) resize.observe(node)
    window.addEventListener('resize', onWindow)
    schedule(el)
  },
  updated(el, binding) {
    const state = bound.get(el)
    if (state) state.opts = binding.value
    schedule(el)
  },
  unmounted(el) {
    const state = bound.get(el)
    if (!state) return
    clearTimeout(state.frame)
    state.resize.disconnect()
    window.removeEventListener('resize', state.onWindow)
    bound.delete(el)
  },
}
