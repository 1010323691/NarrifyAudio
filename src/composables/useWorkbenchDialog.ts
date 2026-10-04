import { nextTick, onBeforeUnmount, onDeactivated, watch, type Ref } from 'vue'

const scrollLocks = new WeakMap<HTMLElement, { count: number; overflow: string }>()

/** Shared focus and scroll handling for the production drawers and dialogs. */
export function useWorkbenchDialog(
  open: Ref<boolean>,
  panel: Ref<HTMLElement | null>,
  close: () => void,
  opener?: () => HTMLElement | null,
) {
  let returnFocus: HTMLElement | null = null
  let scrollHost: HTMLElement | null = null
  function restore() {
    if (scrollHost) {
      const lock = scrollLocks.get(scrollHost)
      if (lock && --lock.count === 0) {
        scrollHost.style.overflow = lock.overflow
        scrollLocks.delete(scrollHost)
      }
      scrollHost = null
    }
    const opener = returnFocus
    // The opener may still be inert until Vue removes the drawer on this tick.
    void nextTick().then(() => {
      if (!open.value && opener?.isConnected) opener.focus()
    })
  }
  watch(
    open,
    async (value) => {
      if (!value) {
        restore()
        return
      }
      returnFocus = opener?.() ?? (document.activeElement as HTMLElement | null)
      scrollHost = document.querySelector<HTMLElement>('.app-main') ?? document.body
      const lock = scrollLocks.get(scrollHost) ?? { count: 0, overflow: scrollHost.style.overflow }
      lock.count++
      scrollLocks.set(scrollHost, lock)
      scrollHost.style.overflow = 'hidden'
      await nextTick()
      if (open.value)
        (
          panel.value?.querySelector<HTMLElement>('button:not([disabled]),input:not([disabled])') ??
          panel.value
        )?.focus()
    },
    { flush: 'post' },
  )
  function keydown(event: KeyboardEvent) {
    if (!open.value || !panel.value) return
    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      close()
      return
    }
    if (event.key !== 'Tab') return
    const controls = [
      ...panel.value.querySelectorAll<HTMLElement>(
        'button:not([disabled]),input:not([disabled]),select:not([disabled]),a[href],[tabindex="0"]',
      ),
    ].filter((el) => el.getClientRects().length)
    const first = controls[0],
      last = controls[controls.length - 1]
    if (!first) {
      event.preventDefault()
      panel.value.focus()
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last?.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }
  onDeactivated(restore)
  onBeforeUnmount(restore)
  return keydown
}
