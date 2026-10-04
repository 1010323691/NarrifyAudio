<template>
  <div class="workbench-context-bar">
    <div class="workbench-context-icon" aria-hidden="true"><slot name="icon" /></div>
    <div class="workbench-context-copy">
      <div class="workbench-context-title"><slot name="title" /></div>
      <div class="workbench-context-description"><slot name="description" /></div>
    </div>
    <div v-if="$slots.metrics" class="workbench-context-metrics"><slot name="metrics" /></div>
    <div v-if="$slots.actions" class="workbench-context-actions"><slot name="actions" /></div>
  </div>
</template>

<style>
.workbench-context-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  min-height: 64px;
  padding: 10px 14px;
  border: 1px solid hsl(var(--border));
  border-radius: 12px;
  background: hsl(var(--card) / 0.95);
}
.workbench-context-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 32px;
  height: 32px;
  overflow: hidden;
  border-radius: 8px;
  background: hsl(var(--primary) / 0.08);
  color: hsl(var(--primary));
}
.workbench-context-icon svg { width: 18px; height: 18px; }
.workbench-context-copy { flex: 1 1 220px; min-width: 0; }
.workbench-context-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; font-weight: 600; line-height: 20px; }
.workbench-context-description { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: hsl(var(--muted-foreground)); font-size: 12px; line-height: 18px; }
.workbench-context-title > p { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.workbench-context-metrics { display: flex; align-items: center; flex-wrap: wrap; gap: 16px; }
.workbench-context-metric { font-size: 12px; line-height: 18px; color: hsl(var(--muted-foreground)); }
.workbench-context-metric strong { display: block; font-size: 16px; line-height: 20px; font-weight: 600; font-variant-numeric: tabular-nums; color: hsl(var(--foreground)); }
.workbench-context-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-left: auto; font-size: 12px; }
.workbench-context-actions button { min-height: 32px; height: 32px; padding-top: 0; padding-bottom: 0; }
.workbench-context-actions label { display: inline-flex; align-items: center; gap: 6px; min-height: 32px; cursor: pointer; }
@media (max-width: 600px) {
  .workbench-context-copy { flex-basis: calc(100% - 44px); }
  .workbench-context-metrics { margin-left: 44px; }
  .workbench-context-actions { width: calc(100% - 44px); margin-left: 44px; justify-content: flex-start; }
}
@media (prefers-reduced-motion: reduce) {
  .workbench-context-bar * { animation: none !important; transition: none !important; }
}
</style>
