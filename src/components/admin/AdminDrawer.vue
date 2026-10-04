<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { X } from 'lucide-vue-next'
defineProps<{ title: string }>()
const emit = defineEmits<{ close: [] }>()
const panel = ref<HTMLElement | null>(null)
const returnFocus = document.activeElement as HTMLElement | null
function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); emit('close') }
  if (event.key !== 'Tab' || !panel.value) return
  const items = [...panel.value.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]')]
  const first = items[0], last = items[items.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
onMounted(async () => { await nextTick(); panel.value?.querySelector<HTMLElement>('button')?.focus() })
onBeforeUnmount(() => { if (returnFocus?.isConnected) returnFocus.focus() })
</script>

<template>
  <div class="admin-drawer-backdrop" @click.self="emit('close')" @keydown="onKeydown">
    <section ref="panel" class="admin-drawer" role="dialog" aria-modal="true" aria-labelledby="admin-drawer-title">
      <header><div><span class="admin-drawer-eyebrow">详细信息</span><h2 id="admin-drawer-title">{{ title }}</h2></div><button class="admin-icon-button" aria-label="关闭详情" @click="emit('close')"><X class="h-5 w-5" /></button></header>
      <div class="admin-drawer-content"><slot /></div>
    </section>
  </div>
</template>
