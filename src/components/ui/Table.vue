<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { cn } from '@/lib/utils'

/** 传入 `pageSize` 即启用固定行高分页盒子：高度恒为「表头 + 10 行」，选 20/50 时盒内滚动。
 *  `rowHeight` 为单行高度（px），表头固定 40px。 */
const props = withDefaults(defineProps<{ class?: string; pageSize?: number; rowHeight?: number }>(), { rowHeight: 48 })
const box = ref<HTMLElement | null>(null)
let observer: ResizeObserver | null = null
// 盒子的边框与横向滚动条都会挤占内部高度：把二者之和补进盒子高度，保证 10 行完整可见。
function measure() {
  const el = box.value
  if (!el || props.pageSize === undefined) return
  el.style.removeProperty('--ft-extra')
  const extra = el.offsetHeight - el.clientHeight
  if (extra > 0) el.style.setProperty('--ft-extra', `${extra}px`)
}
onMounted(() => {
  observer = new ResizeObserver(measure)
  if (box.value) { observer.observe(box.value); const table = box.value.querySelector('table'); if (table) observer.observe(table) }
  measure()
})
watch(() => props.pageSize, measure, { flush: 'post' })
onBeforeUnmount(() => observer?.disconnect())
</script>

<template>
  <div
    ref="box"
    :class="cn('relative w-full overflow-auto', pageSize !== undefined && 'ui-fixed-table', pageSize !== undefined && pageSize > 10 && 'is-scroll', props.class)"
    :style="pageSize !== undefined ? { '--ft-row': `${rowHeight}px` } : undefined"
  >
    <table class="w-full caption-bottom text-sm">
      <slot />
    </table>
  </div>
</template>
