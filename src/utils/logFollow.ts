import { ref, watch } from 'vue'

/** How close to the bottom (px) still counts as "following" the tail. */
const NEAR_BOTTOM_PX = 48

/** True if the scroll element's viewport sits at (or just above) its bottom edge. */
export function isNearBottom(el: HTMLElement, threshold = NEAR_BOTTOM_PX): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= threshold
}

/**
 * Follow a growing log list (or string buffer) to its bottom while the user reads
 * the tail. Works for anything exposing a numeric `.length` (the log array, the raw
 * LLM stream, …).
 *
 * Returns a function to bind as the `ref` of the scrollable element — a native
 * element, or a component such as `ScrollArea` (its root element is used). On new
 * content it scrolls to the bottom *only* if the user was already near the bottom,
 * so a manual scroll up to inspect older lines is never yanked back.
 *
 * The near-bottom check runs in a pre-render `watch` (so it sees the scroll position
 * *before* the new line grows the box); the actual scroll is deferred to the next
 * animation frame, after Vue has painted the new line.
 */
export function useLogAutoFollow(getSignal: () => { length: number }) {
  const el = ref<HTMLElement | null>(null)

  function bind(target: unknown) {
    // A native element is the element itself; a component instance exposes its root
    // via $el. (target is null on unmount.)
    const t = target as { $el?: HTMLElement } | null
    el.value = t?.$el ?? (t as HTMLElement) ?? null
  }

  watch(
    () => getSignal().length,
    () => {
      const node = el.value
      if (node && isNearBottom(node)) {
        requestAnimationFrame(() => {
          if (node) node.scrollTop = node.scrollHeight
        })
      }
    },
  )

  return { bind }
}
