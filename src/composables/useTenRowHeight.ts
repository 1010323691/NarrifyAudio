import { computed, nextTick, onBeforeUnmount, onMounted, ref, toValue, watch, type MaybeRefOrGetter, type Ref } from 'vue'

/** Share the voices workbench's ten-row fit without adding another scroll container. */
export function useTenRowHeight(table: Ref<HTMLTableElement | null>, pageSize: MaybeRefOrGetter<number>) {
  const fittedHeight = ref(32)
  let observer: ResizeObserver | null = null
  function measure() {
    const element = table.value
    const viewportHeight = element?.parentElement?.getBoundingClientRect().height ?? 0
    if (!viewportHeight || !element?.tHead) return
    const bodyHeight = viewportHeight - element.tHead.getBoundingClientRect().height - 0.5
    const minimum = window.matchMedia('(pointer:coarse)').matches ? 51 : 32
    // Round down to Chromium's layout unit so the last row stays inside the viewport.
    fittedHeight.value = Math.max(minimum, Math.floor(bodyHeight / 10 * 64) / 64)
  }
  onMounted(() => {
    observer = new ResizeObserver(measure)
    watch(table, element => {
      observer?.disconnect()
      if (element?.parentElement) observer?.observe(element.parentElement)
      if (element?.tHead) observer?.observe(element.tHead)
      measure()
    }, { immediate: true, flush: 'post' })
  })
  watch(() => toValue(pageSize), async () => { await nextTick(); measure() })
  onBeforeUnmount(() => observer?.disconnect())
  return computed(() => toValue(pageSize) === 10 ? fittedHeight.value : 32)
}
