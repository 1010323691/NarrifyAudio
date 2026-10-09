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
  /** 行高可放大到的上限：视口高于「默认行高」所需时，把父容器剩余高度平摊到各行，避免列表下方留白 */
  grow?: number
}

const STEP = 64

/**
 * 收缩算法本体（不依赖 DOM，便于单测）：从 `max` 起，每轮把当前溢出量平摊到 `rows` 行上缩小行高，
 * 最多 3 轮，不低于 `min`；`measure()` 返回在当前行高下母容器的最大溢出像素。
 */
export function solveRowHeight(
  max: number,
  min: number,
  rows: number,
  measure: (height: number) => number,
): number {
  let height = max
  for (let i = 0; i < 3; i++) {
    const overflow = measure(height)
    if (overflow <= 0.5 || height <= min) break
    height = Math.max(min, Math.floor((height - overflow / rows) * STEP) / STEP)
  }
  return height
}

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

/** 父容器（纵向 flex/块）里除本盒子外的空闲高度；父容器高度不确定时返回 0。 */
function freeSpace(el: HTMLElement): number {
  const parent = el.parentElement
  if (!parent) return 0
  const style = getComputedStyle(parent)
  const gap = parseFloat(style.rowGap) || 0
  const kids = Array.from(parent.children).filter((c) => getComputedStyle(c).position !== 'absolute')
  const used = kids.reduce((sum, c) => sum + c.getBoundingClientRect().height, 0) + gap * Math.max(0, kids.length - 1)
  const inner = parent.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom)
  return Math.max(0, inner - used)
}

function fit(el: HTMLElement, options: FitRowsOptions) {
  if (!el.isConnected || !el.offsetParent) return
  const rows = options.rows ?? 10
  const min = options.min ?? 24
  const max = options.max ?? declaredMax(el, options.prop, 40)
  const ancestors = clippingAncestors(el)
  const apply = (height: number) => el.style.setProperty(options.prop, `${height}px`)
  const previous = parseFloat(el.style.getPropertyValue(options.prop))
  const next = solveRowHeight(max, min, rows, (height) => {
    apply(height)
    return maxOverflow(ancestors)
  })
  // 迟滞：Firefox 的亚像素/滚动条取整会让结果在相邻两个值间来回跳，每次变化又触发
  // ResizeObserver 重新适配，列表就持续抽动。变化不足 1px 时沿用上次行高。
  let target = next
  if (options.grow && next >= max) {
    const grown = Math.min(options.grow, Math.floor((max + freeSpace(el) / rows) * STEP) / STEP)
    apply(grown)
    // 放大后若让祖先溢出（测量误差），回退到不放大。
    target = grown > max && maxOverflow(ancestors) <= 0.5 ? grown : max
  }
  apply(Number.isFinite(previous) && Math.abs(previous - target) < 1 ? previous : target)
  // 祖先链可能在挂载之后变化（条件渲染的外层容器）：每次适配后补挂观察，observe 对同一节点幂等。
  const state = bound.get(el)
  if (state) for (const node of ancestors.slice(0, 2)) state.resize.observe(node)
}

interface Bound { frame: number; burst: number[]; resize: ResizeObserver; onWindow: () => void; opts: FitRowsOptions }
const bound = new WeakMap<HTMLElement, Bound>()

function schedule(el: HTMLElement) {
  const state = bound.get(el)
  if (!state || state.frame) return
  // 断路器：1 秒内连续适配超过 8 次说明布局在自激振荡，停止观察触发的重排，待窗口缩放/更新再恢复。
  const now = Date.now()
  state.burst = state.burst.filter((t) => now - t < 1000)
  if (state.burst.length >= 8) return
  state.burst.push(now)
  // setTimeout 而非 rAF：后台/未绘制的标签页 rAF 不触发，行高会停在未适配状态。
  state.frame = window.setTimeout(() => {
    state.frame = 0
    fit(el, state.opts)
  }, 30)
}

export const vFitRows: Directive<HTMLElement, FitRowsOptions> = {
  mounted(el, binding) {
    const resize = new ResizeObserver(() => schedule(el))
    const onWindow = () => {
      const state = bound.get(el)
      if (state) state.burst = []
      schedule(el)
    }
    bound.set(el, { frame: 0, burst: [], resize, onWindow, opts: binding.value })
    for (const node of clippingAncestors(el).slice(0, 2)) resize.observe(node)
    // 盒子自身从隐藏（display:none，尺寸为 0）变为可见时也要重新适配。
    resize.observe(el)
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
