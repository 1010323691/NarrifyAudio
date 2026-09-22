<script setup lang="ts">
import { onBeforeUnmount, watch } from 'vue'
import { toasts, useToast, type ToastItem } from './toast'
import { cn } from '@/lib/utils'
import { CheckCircle2, AlertCircle, Info, X } from 'lucide-vue-next'

const { dismiss } = useToast()
const timers = new Map<number, ReturnType<typeof setTimeout>>()

const styles: Record<NonNullable<ToastItem['variant']>, string> = {
  default: 'border-border bg-card text-foreground',
  destructive: 'border-destructive/50 bg-destructive/10 text-destructive',
  success: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
}

function iconFor(t: ToastItem) {
  if (t.variant === 'destructive') return AlertCircle
  if (t.variant === 'success') return CheckCircle2
  return Info
}

// Schedule a dismiss timer for each toast that doesn't have one yet.
watch(
  toasts,
  (list) => {
    for (const t of list) {
      if (t.duration > 0 && !timers.has(t.id)) {
        timers.set(
          t.id,
          setTimeout(() => {
            dismiss(t.id)
            timers.delete(t.id)
          }, t.duration),
        )
      }
    }
  },
  { deep: true, immediate: true },
)

function close(t: ToastItem) {
  const timer = timers.get(t.id)
  if (timer) clearTimeout(timer)
  timers.delete(t.id)
  dismiss(t.id)
}

onBeforeUnmount(() => timers.forEach(clearTimeout))
</script>

<template>
  <div class="pointer-events-none fixed bottom-4 right-4 z-50 flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2">
    <TransitionGroup enter-active-class="transition duration-200" leave-active-class="transition duration-150">
      <div
        v-for="t in toasts"
        :key="t.id"
        :class="cn('pointer-events-auto flex items-start gap-3 rounded-lg border p-3 shadow-lg', styles[t.variant ?? 'default'])"
      >
        <component :is="iconFor(t)" class="mt-0.5 h-5 w-5 shrink-0" />
        <div class="flex-1 space-y-0.5 text-sm">
          <p class="font-medium">{{ t.title }}</p>
          <p v-if="t.description" class="text-muted-foreground">{{ t.description }}</p>
        </div>
        <button
          class="shrink-0 rounded p-0.5 opacity-60 transition hover:opacity-100"
          aria-label="关闭"
          @click="close(t)"
        >
          <X class="h-4 w-4" />
        </button>
      </div>
    </TransitionGroup>
  </div>
</template>
