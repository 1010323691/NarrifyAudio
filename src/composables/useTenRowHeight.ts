import { computed, nextTick, onBeforeUnmount, onMounted, ref, toValue, watch, type MaybeRefOrGetter, type Ref } from 'vue'

/** Fit ten rows in the existing scroller, retaining the normal density on taller screens. */
export function useTenRowHeight(table: Ref<HTMLTableElement | null>, pageSize: MaybeRefOrGetter<number>) {
  const fittedHeight = ref(40)
  let observer: ResizeObserver | null = null
  let mutationObserver: MutationObserver | null = null
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
    let fitted = Math.max(minimum, Math.min(normal, Math.floor(bodyHeight / 10 * 64) / 64))
    // Rows taller than the fit band's 27px content assumption (e.g. merge's
    // 28px row action buttons) render at H + excess inside 27-37px, so the
    // fill-the-space value alone overflows: pull the variable down by the
    // excess to keep ten rendered rows inside the viewport.
    const rows = element.tBodies[0]?.rows
    if (rows?.length) {
      // Restore the consumer's inline value afterwards: it is the source of the
      // current rows' sizing, and Vue will not re-apply an unchanged binding.
      const prior = element.style.getPropertyValue('--list-row-height')
      element.style.setProperty('--list-row-height', '1px')
      const natural = Math.max(...Array.from(rows, row => row.getBoundingClientRect().height))
      if (prior) element.style.setProperty('--list-row-height', prior)
      else element.style.removeProperty('--list-row-height')
      const excess = Math.max(0, natural - 27)
      if (excess && fitted > 27 + excess) fitted = Math.max(minimum, Math.floor((fitted - excess) * 64) / 64)
    }
    fittedHeight.value = fitted
  }
  onMounted(() => {
    observer = new ResizeObserver(measure)
    watch(table, element => {
      observer?.disconnect()
      mutationObserver?.disconnect()
      if (element?.parentElement) observer?.observe(element.parentElement)
      if (element?.tHead) observer?.observe(element.tHead)
      // Rows and their container arrive after the table mounts (the skeleton
      // tbody is replaced by the data tbody when rows load): watch the table
      // subtree so any row or tbody replacement re-fits the height.
      mutationObserver = new MutationObserver(measure)
      if (element) mutationObserver.observe(element, { childList: true, subtree: true })
      measure()
    }, { immediate: true, flush: 'post' })
  })
  watch(() => toValue(pageSize), async () => { await nextTick(); measure() })
  onBeforeUnmount(() => { observer?.disconnect(); mutationObserver?.disconnect() })
  return computed(() => toValue(pageSize) === 10 ? `${fittedHeight.value}px` : undefined)
}
