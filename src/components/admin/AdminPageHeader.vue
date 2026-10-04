<script setup lang="ts">
defineProps<{ title: string; description: string }>()
</script>

<template>
  <header class="admin-page-header">
    <div><h1>{{ title }}</h1><p>{{ description }}</p></div>
    <div class="admin-page-actions"><slot /></div>
  </header>
</template>
