import { computed, nextTick, onBeforeUnmount, onMounted, ref, toValue, watch, type MaybeRefOrGetter, type Ref } from 'vue'

/** Fit ten rows in the existing scroller, retaining the normal density on taller screens. */
export function useTenRowHeight(table: Ref<HTMLTableElement | null>, pageSize: MaybeRefOrGetter<number>) {
  const fittedHeight = ref(40)
  let observer: ResizeObserver | null = null
  function measure() {
    const element = table.value
    const viewport = element?.parentElement
    if (!viewport?.clientHeight || !element?.tHead) return
    const style = getComputedStyle(viewport)
    const padding = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom)
    const lastCell = element.tBodies[0]?.rows[0]?.cells[0]
    // clientHeight excludes borders and the horizontal scrollbar. A collapsed
    // table still paints half of its final cell border below the last row.
    const bottomBorder = lastCell ? parseFloat(getComputedStyle(lastCell).borderBottomWidth) / 2 : 0.5
    const bodyHeight = viewport.clientHeight - padding - element.tHead.getBoundingClientRect().height - bottomBorder
    const coarse = window.matchMedia('(pointer:coarse)').matches
    const minimum = coarse ? 55 : 28
    const normal = coarse ? 55 : 40
    // Round down to Chromium's layout unit so the last row stays inside the viewport.
    fittedHeight.value = Math.max(minimum, Math.min(normal, Math.floor(bodyHeight / 10 * 64) / 64))
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
  return computed(() => toValue(pageSize) === 10 ? `${fittedHeight.value}px` : undefined)
}
