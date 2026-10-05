<template>
  <div class="workbench-actionbar">
    <div class="workbench-action-summary"><slot name="summary" /></div>
    <div class="workbench-action-buttons"><slot /></div>
  </div>
</template>

<style scoped>
.workbench-actionbar { display:flex; flex-wrap:wrap; align-items:center; gap:12px; flex-shrink:0; padding:12px; border:1px solid hsl(var(--border)); border-radius:12px; background:hsl(var(--card) / .95); }
.workbench-action-summary { flex:1 1 240px; min-width:0; font-size:12px; overflow-wrap:anywhere; }
.workbench-action-buttons { display:flex; flex-wrap:wrap; align-items:center; justify-content:flex-end; gap:8px; min-width:0; }
.workbench-action-buttons :deep(button) { height:36px; font-size:12px; }
@media(max-width:1100px) { .workbench-action-buttons { flex:1 1 100%; } }
</style>
