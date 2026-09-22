import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** Merge Tailwind class lists, resolving conflicts (shadcn-vue convention). */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
