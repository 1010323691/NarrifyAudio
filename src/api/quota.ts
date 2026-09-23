import { http } from './client'

export interface QuotaBalance {
  available_units: number
  reserved_units: number
  frozen_units: number
  consumed_units: number
  unit?: string
}

export interface QuotaTransaction {
  id: string
  task_id?: string | null
  amount: number
  kind: string
  note: string
  available_before?: number | null
  available_after?: number | null
  reserved_before?: number | null
  reserved_after?: number | null
  consumed_before?: number | null
  consumed_after?: number | null
  created_at: string
  resource_type?: 'LLM' | 'TTS' | null
  operation_type?: string | null
  char_count?: number | null
}

export function getQuota(): Promise<QuotaBalance> {
  return http.get('/api/v1/quota')
}

export function listQuotaTransactions(): Promise<QuotaTransaction[]> {
  return http.get('/api/v1/quota/transactions')
}
