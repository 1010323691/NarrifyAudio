<script setup lang="ts">
import { cva } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const alertVariants = cva('relative flex items-start gap-3 rounded-lg border p-3 text-sm', {
  variants: {
    variant: {
      default: 'border-border bg-card text-foreground',
      destructive: 'border-destructive/50 bg-destructive/10 text-destructive',
      info: 'border-primary/30 bg-primary/10 text-primary',
      warning: 'border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-400',
    },
  },
  defaultVariants: { variant: 'default' },
})

interface Props {
  variant?: 'default' | 'destructive' | 'info' | 'warning'
  class?: string
}
const props = withDefaults(defineProps<Props>(), { variant: 'default' })
</script>

<template>
  <div :class="cn(alertVariants({ variant }), props.class)">
    <slot name="icon" />
    <div class="flex-1 space-y-1">
      <slot />
      <div v-if="$slots.description" class="text-muted-foreground">
        <slot name="description" />
      </div>
    </div>
  </div>
</template>
