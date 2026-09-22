import { http } from './client'

export interface AdminUser {
  id: string
  email: string
  username: string
  display_name: string
  role: 'user' | 'admin'
  is_active: boolean
  created_at: string
}

export interface StorageSettings {
  root_path: string
  source: 'admin' | 'deployment-default'
}

export function listUsers(): Promise<AdminUser[]> {
  return http.get('/api/v1/admin/users')
}

export function updateUser(id: string, payload: { is_active?: boolean; role?: 'user' | 'admin' }): Promise<{ id: string; is_active: boolean; role: 'user' | 'admin' }> {
  return http.patch(`/api/v1/admin/users/${id}`, payload)
}

export function getStorageSettings(): Promise<StorageSettings> {
  return http.get('/api/v1/admin/settings/storage')
}

export function updateStorageRoot(root_path: string): Promise<StorageSettings> {
  return http.patch('/api/v1/admin/settings/storage', { root_path })
}

export function adjustQuota(userId: string, amount: number, idempotency_key: string, note = ''): Promise<Record<string, number | string>> {
  return http.post(`/api/v1/admin/users/${userId}/quota/adjust`, { amount, idempotency_key, note })
}
