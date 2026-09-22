<script setup lang="ts">
import { cn } from '@/lib/utils'

const props = defineProps<{ class?: string; modelValue?: string | number }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: string | number): void }>()
</script>

<template>
  <select
    v-bind="$attrs"
    :value="modelValue"
    :class="cn(
      'flex h-10 w-full rounded-lg border border-input bg-background px-3 py-2 text-sm shadow-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-50',
      props.class,
    )"
    @change="emit('update:modelValue', ($event.target as HTMLSelectElement).value)"
  >
    <!-- 原生 <select> 的选项来自父组件的默认插槽（<option>…），必须在此渲染。 -->
    <slot />
  </select>
</template>
