import { http } from './client'
import type { BookAnalyzeResult, BookSmartSplitResult, BookSplitResult } from '@/types'

/** Preview the split: chapters + per-chapter filenames (no files written). */
export function analyzeBook(path: string): Promise<BookAnalyzeResult> {
  return http.post<BookAnalyzeResult>('/api/book/analyze', { path })
}

/** Write files to the workspace's 02_split_text/: by the smart-repair result
 *  (`smart` — the backend re-runs the deterministic repair and writes the same
 *  `第 NNN 章 标题.txt` files as smart-split) or a single `<base> 全书.txt`
 *  (wholeBook, the zero-chapter fallback). */
export function splitBook(
  path: string,
  opts: { base?: string; wholeBook?: boolean; smart?: boolean } = {},
): Promise<BookSplitResult> {
  return http.post<BookSplitResult>('/api/book/split', {
    path,
    base: opts.base ?? null,
    whole_book: !!opts.wholeBook,
    smart: !!opts.smart,
  })
}

/** Smart recognition: mechanically repair the chapter structure (renumber
 *  1..N in physical order, split abnormally long chapters, drop exact
 *  duplicate chapters) and write one file per repaired chapter as
 *  ``第 NNN 章 标题.txt`` into 02_split_text/. Always outputs a repair report. */
export function smartSplitBook(path: string): Promise<BookSmartSplitResult> {
  return http.post<BookSmartSplitResult>('/api/book/smart-split', { path })
}
