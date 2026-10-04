<script setup lang="ts">
import { cn } from '@/lib/utils'
import { Loader2 } from 'lucide-vue-next'

const props = defineProps<{ modelValue: boolean; class?: string; disabled?: boolean; small?: boolean; busy?: boolean }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>()

function toggle() {
  if (props.disabled || props.busy) return
  emit('update:modelValue', !props.modelValue)
}
</script>

<template>
  <button
    type="button"
    role="switch"
    :aria-checked="modelValue"
    :disabled="disabled || busy"
    :aria-busy="busy || undefined"
    @click="toggle"
    :class="cn(
      'ui-switch inline-flex shrink-0 cursor-pointer items-center rounded-full border-2 border-transparent px-0.5 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-50',
      props.small ? 'h-6 w-10' : 'h-6 w-11',
      modelValue ? 'bg-primary' : 'bg-input',
      props.class,
    )"
  >
    <span
      :class="cn(
        'ui-switch-thumb pointer-events-none flex h-5 w-5 items-center justify-center rounded-full bg-white shadow ring-0 transition-transform',
        modelValue ? (props.small ? 'translate-x-4' : 'translate-x-5') : 'translate-x-0',
      )"
    ><Loader2 v-if="busy" class="h-3 w-3 animate-spin text-primary" aria-hidden="true" /></span>
  </button>
</template>
