<script setup lang="ts">
import { ref } from 'vue'
import { Info, Table2, LineChart } from 'lucide-vue-next'

export interface ChartTableColumn { key: string; label: string; format?: (value: any) => string }
const props = defineProps<{
  title: string
  subtitle?: string
  /** Data-scope note shown as a hover hint next to the title. */
  hint?: string
  /** Optional table view of the plotted data (accessibility / exact values). */
  columns?: ChartTableColumn[]
  rows?: Record<string, any>[]
  span?: 3 | 4 | 5 | 6 | 7 | 8 | 12
}>()
const showTable = ref(false)
</script>

<template>
  <section class="chart-card" :class="span ? `span-${span}` : 'span-6'">
    <header class="chart-card__header">
      <div class="chart-card__titles">
        <h3>{{ title }}<span v-if="hint" class="chart-card__hint" :title="hint" tabindex="0" :aria-label="hint"><Info class="h-3.5 w-3.5" /></span></h3>
        <p v-if="subtitle">{{ subtitle }}</p>
      </div>
      <div class="chart-card__actions">
        <slot name="actions" />
        <button v-if="props.columns && props.rows" type="button" class="admin-icon-button" :aria-pressed="showTable"
                :title="showTable ? '显示图表' : '显示数据表'" :aria-label="showTable ? '显示图表' : '显示数据表'" @click="showTable = !showTable">
          <LineChart v-if="showTable" class="h-4 w-4" /><Table2 v-else class="h-4 w-4" />
        </button>
      </div>
    </header>
    <div class="chart-card__body">
      <div v-if="showTable && props.columns && props.rows" class="admin-table chart-card__table" tabindex="0" role="region" :aria-label="`${title} 数据表`">
        <table class="compact-table">
          <thead><tr><th v-for="column in props.columns" :key="column.key">{{ column.label }}</th></tr></thead>
          <tbody><tr v-for="(row, index) in props.rows" :key="index"><td v-for="column in props.columns" :key="column.key">{{ column.format ? column.format(row[column.key]) : row[column.key] ?? '—' }}</td></tr></tbody>
        </table>
      </div>
      <slot v-else />
    </div>
    <footer v-if="$slots.footer" class="chart-card__footer"><slot name="footer" /></footer>
  </section>
</template>
