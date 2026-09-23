import { http } from './client'

export interface AuthUser {
  id: string
  email: string
  username: string
  display_name: string
  role: 'user' | 'admin'
  is_active: boolean
}

export interface AuthResponse {
  user: AuthUser
  csrf_token: string | null
}

export function register(payload: { email: string; password: string; username: string; display_name: string }): Promise<AuthResponse> {
  return http.post('/api/auth/register', payload)
}

export function login(payload: { identifier: string; password: string }): Promise<AuthResponse> {
  return http.post('/api/auth/login', payload)
}

export function currentUser(): Promise<AuthResponse> {
  return http.get('/api/auth/me')
}

export function logout(): Promise<{ ok: boolean }> {
  return http.post('/api/auth/logout')
}
