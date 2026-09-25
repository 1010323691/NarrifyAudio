<script setup lang="ts">
import { cva } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const buttonVariants = cva(
  'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg text-sm font-semibold transition-all duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50',
  {
    variants: {
      variant: {
        default:
          'bg-primary text-primary-foreground shadow-md shadow-primary/25 hover:shadow-lg hover:shadow-primary/30 hover:bg-primary/90 hover:-translate-y-px active:translate-y-0 active:shadow-md',
        destructive:
          'bg-destructive text-destructive-foreground shadow-md shadow-destructive/25 hover:shadow-lg hover:shadow-destructive/30 hover:bg-destructive/90 active:translate-y-px',
        outline:
          'border border-border/70 bg-white/40 text-foreground shadow-sm backdrop-blur-sm hover:border-primary/40 hover:bg-accent hover:text-accent-foreground active:translate-y-px dark:bg-card/40',
        secondary: 'bg-secondary text-secondary-foreground hover:bg-secondary/80 active:translate-y-px',
        ghost: 'hover:bg-accent hover:text-accent-foreground',
        link: 'text-primary underline-offset-4 hover:underline',
      },
      size: {
        default: 'h-10 px-4 py-2',
        sm: 'h-9 rounded-lg px-3 text-xs',
        lg: 'h-11 rounded-lg px-6',
        icon: 'h-10 w-10',
      },
    },
    defaultVariants: { variant: 'default', size: 'default' },
  },
)

interface Props {
  // Explicit literal unions (not `extends VariantProps<…>`), which the Vue SFC
  // compiler can resolve at build time — extending the CVA generic fails.
  variant?: 'default' | 'destructive' | 'outline' | 'secondary' | 'ghost' | 'link'
  size?: 'default' | 'sm' | 'lg' | 'icon'
  class?: string
}
const props = withDefaults(defineProps<Props>(), { variant: 'default', size: 'default' })
</script>

<template>
  <button :class="cn(buttonVariants({ variant, size }), props.class)" v-bind="$attrs">
    <slot />
  </button>
</template>
