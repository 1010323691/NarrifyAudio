<script setup lang="ts" generic="T extends string | number">
defineProps<{ modelValue: T; options: readonly (readonly [T, string])[]; label: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: T] }>()
</script>

<template>
  <div class="admin-segmented" role="radiogroup" :aria-label="label">
    <button v-for="[value, text] in options" :key="String(value)" type="button" role="radio"
            :aria-checked="modelValue === value" :class="{ 'is-active': modelValue === value }"
            @click="emit('update:modelValue', value)">{{ text }}</button>
  </div>
</template>
