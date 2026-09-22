<script setup lang="ts">
import { cva } from 'class-variance-authority'
import { cn } from '@/lib/utils'

// Explicit literal-union props (not `extends VariantProps<…>`): the Vue SFC
// compiler can't resolve the CVA generic at build time.
const badgeVariants = cva(
  'inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] font-semibold leading-none tracking-wide transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2',
  {
    variants: {
      variant: {
        default: 'border-transparent bg-primary text-primary-foreground shadow-sm hover:bg-primary/80',
        secondary: 'border-transparent bg-secondary text-secondary-foreground hover:bg-secondary/80',
        destructive: 'border-transparent bg-destructive text-destructive-foreground shadow hover:bg-destructive/80',
        success:
          'border-transparent bg-emerald-500/15 text-emerald-600 hover:bg-emerald-500/25 dark:text-emerald-400',
        warning:
          'border-transparent bg-amber-500/15 text-amber-600 hover:bg-amber-500/25 dark:text-amber-400',
        outline: 'border-border text-foreground hover:bg-accent hover:text-accent-foreground',
      },
    },
    defaultVariants: { variant: 'default' },
  },
)

interface Props {
  variant?: 'default' | 'secondary' | 'destructive' | 'success' | 'warning' | 'outline'
  class?: string
}
const props = withDefaults(defineProps<Props>(), { variant: 'default' })
</script>

<template>
  <span :class="cn(badgeVariants({ variant }), props.class)">
    <slot />
  </span>
</template>
