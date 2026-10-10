import { onBeforeUnmount, onMounted, ref, type Ref } from 'vue'

/** Tracks an element's pixel size so SVG charts can lay out ticks in real units. */
export function useChartSize(el: Ref<HTMLElement | null>) {
  const width = ref(0)
  const height = ref(0)
  let observer: ResizeObserver | null = null
  function read() {
    width.value = el.value?.clientWidth ?? 0
    height.value = el.value?.clientHeight ?? 0
  }
  onMounted(() => {
    if (!el.value) return
    read()
    observer = new ResizeObserver(read)
    observer.observe(el.value)
  })
  onBeforeUnmount(() => observer?.disconnect())
  return { width, height }
}

/** Pointer position relative to `el`'s top-left corner. */
export function pointerIn(event: PointerEvent, el: HTMLElement | null): { x: number; y: number } {
  const box = el?.getBoundingClientRect()
  return { x: event.clientX - (box?.left ?? 0), y: event.clientY - (box?.top ?? 0) }
}
