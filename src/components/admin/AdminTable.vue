<script setup lang="ts">
withDefaults(defineProps<{ tableClass?: string; label?: string }>(), {
  label: '管理数据表，可横向滚动',
})
</script>

<template>
  <div class="admin-table" tabindex="0" role="region" :aria-label="label">
    <table :class="tableClass"><slot /></table>
  </div>
</template>
