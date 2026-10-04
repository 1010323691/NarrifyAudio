<template>
  <div class="admin-loading-state" role="status" aria-label="正在加载管理数据">
    <span class="sr-only">正在加载管理数据…</span>
    <div class="admin-loading-metrics"><div v-for="i in 4" :key="i" /></div>
    <div class="admin-loading-table"><div v-for="i in 6" :key="i" /></div>
  </div>
</template>
