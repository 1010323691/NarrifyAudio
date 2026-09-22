<script setup lang="ts">
import { cn } from '@/lib/utils'

const props = defineProps<{ class?: string; modelValue?: string | number }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: string | number): void }>()
</script>

<template>
  <input
    v-bind="$attrs"
    :value="modelValue"
    :class="cn(
      'flex h-10 w-full rounded-lg border border-input bg-background px-3 py-2 text-sm shadow-none transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-50',
      props.class,
    )"
    @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
  />
</template>
