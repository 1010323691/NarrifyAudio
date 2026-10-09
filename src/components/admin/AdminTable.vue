<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

/** `pageSize` 传入即启用固定行高分页盒子：高度恒为「表头 + 10 行」，选 20/50 时内部滚动。 */
const props = withDefaults(defineProps<{ tableClass?: string; label?: string; pageSize?: number }>(), {
  label: '管理数据表，可横向滚动',
})
const box = ref<HTMLElement | null>(null)
let observer: ResizeObserver | null = null
// 窄屏出现横向滚动条时它会挤占盒子高度，导致第 10 行被截；把滚动条厚度补进盒子高度。
function measure() {
  const el = box.value
  if (!el || props.pageSize === undefined) return
  el.style.removeProperty('--admin-hbar')
  const bar = el.scrollWidth > el.clientWidth ? el.offsetHeight - el.clientHeight : 0
  if (bar > 0) el.style.setProperty('--admin-hbar', `${bar}px`)
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
  <div ref="box" class="admin-table" :class="{ 'is-fixed': pageSize !== undefined, 'is-scroll': (pageSize ?? 0) > 10 }" tabindex="0" role="region" :aria-label="label">
    <table :class="tableClass"><slot /></table>
  </div>
</template>
