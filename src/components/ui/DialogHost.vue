<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { dialogState, settleDialog } from '@/components/ui/dialog'

const panel = ref<HTMLElement | null>(null)
const promptInput = ref<HTMLInputElement | null>(null)
const cancelButton = ref<HTMLButtonElement | null>(null)
const confirmButton = ref<HTMLButtonElement | null>(null)
let returnFocus: HTMLElement | null = null

watch(() => dialogState.current, async (current, previous) => {
  if (current && !previous) returnFocus = document.activeElement as HTMLElement | null
  if (!current && previous) {
    await nextTick()
    if (returnFocus?.isConnected) returnFocus.focus()
    returnFocus = null
    return
  }
  if (!current) return
  await nextTick()
  if (current.kind === 'prompt') promptInput.value?.focus()
  else if (current.kind === 'confirm' && !current.hideCancel) cancelButton.value?.focus()
  else confirmButton.value?.focus()
}, { flush: 'post' })

function closeAsCancel() {
  const current = dialogState.current
  if (!current) return
  settleDialog(current.kind === 'confirm' ? false : null)
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    closeAsCancel()
    return
  }
  if (event.key !== 'Tab' || !panel.value) return
  const focusable = [...panel.value.querySelectorAll<HTMLElement>(
    'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )]
  if (!focusable.length) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.value)) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

function acceptDialog() {
  const current = dialogState.current
  if (!current) return
  if (current.kind === 'prompt') settleDialog(current.inputValue)
  else settleDialog(true)
}

function updatePrompt(event: Event) {
  if (dialogState.current?.kind === 'prompt') {
    dialogState.current.inputValue = (event.target as HTMLInputElement).value
  }
}

onBeforeUnmount(() => {
  if (returnFocus?.isConnected) returnFocus.focus()
})
</script>

<template>
  <div
    v-if="dialogState.current"
    class="ui-dialog-host fixed inset-0 z-[100] flex items-center justify-center bg-black/55 p-4 backdrop-blur-[2px]"
    @keydown="onKeydown"
    @click.self="closeAsCancel"
  >
    <section
      ref="panel"
      class="glass-strong w-full max-w-md rounded-2xl p-5 focus:outline-none sm:p-6"
      :role="dialogState.current.kind === 'prompt' ? 'dialog' : 'alertdialog'"
      aria-modal="true"
      :aria-labelledby="`app-dialog-title-${dialogState.current.id}`"
      :aria-describedby="`app-dialog-message-${dialogState.current.id}`"
      tabindex="-1"
    >
      <h2 :id="`app-dialog-title-${dialogState.current.id}`" class="text-base font-semibold text-foreground">
        {{ dialogState.current.title }}
      </h2>
      <p :id="`app-dialog-message-${dialogState.current.id}`" class="mt-2 whitespace-pre-line text-sm leading-6 text-muted-foreground">
        {{ dialogState.current.message }}
      </p>
      <label v-if="dialogState.current.kind === 'prompt'" class="mt-4 grid gap-1.5 text-sm font-medium text-foreground">
        {{ dialogState.current.inputLabel }}
        <input
          ref="promptInput"
          :value="dialogState.current.inputValue"
          :placeholder="dialogState.current.placeholder"
          class="h-11 w-full rounded-lg border border-input bg-background px-3 text-sm text-foreground shadow-sm outline-none transition focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          @input="updatePrompt"
          @keydown.enter.prevent="acceptDialog"
        />
      </label>
      <div class="mt-6 flex justify-end gap-2">
        <button
          v-if="!dialogState.current.hideCancel"
          ref="cancelButton"
          type="button"
          class="inline-flex h-11 items-center justify-center rounded-lg border border-input bg-card px-4 text-sm font-semibold text-foreground shadow-sm transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50"
          @click="closeAsCancel"
        >
          {{ dialogState.current.cancelText }}
        </button>
        <button
          ref="confirmButton"
          type="button"
          class="inline-flex h-11 items-center justify-center rounded-lg px-4 text-sm font-semibold shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          :class="dialogState.current.destructive ? 'bg-destructive text-destructive-foreground hover:bg-destructive/90' : 'bg-primary text-primary-foreground hover:bg-primary/90'"
          @click="acceptDialog"
        >
          {{ dialogState.current.confirmText }}
        </button>
      </div>
    </section>
  </div>
</template>
