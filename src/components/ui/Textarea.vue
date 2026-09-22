<script setup lang="ts">
import { cn } from '@/lib/utils'

const props = defineProps<{ class?: string; modelValue?: string }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: string): void }>()
</script>

<template>
  <textarea
    v-bind="$attrs"
    :value="modelValue"
    :class="cn(
      'flex min-h-[88px] w-full rounded-lg border border-input bg-background px-3 py-2.5 text-sm leading-6 shadow-none placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-50',
      props.class,
    )"
    @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
  />
</template>
