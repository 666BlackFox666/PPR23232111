import type { AppUser, Capabilities, DashboardSummary, ImportMode, ImportPreviewResponse, NotificationListResponse, PprCard, PprListQuery, PprListResponse, PprNotification, UserCreatePayload, UserUpdatePayload } from './types'

declare global {
  interface Window {
    Telegram?: {
      WebApp?: {
        initData?: string
        initDataUnsafe?: {
          start_param?: string
        }
        ready?: () => void
        expand?: () => void
      }
    }
  }
}

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export type AuthMode = 'telegram' | 'dev' | 'none'

export function getTelegramInitData() {
  return window.Telegram?.WebApp?.initData || ''
}

export function getAuthMode(): AuthMode {
  if (getTelegramInitData()) return 'telegram'
  return import.meta.env.DEV && import.meta.env.VITE_DEV_TELEGRAM_ID ? 'dev' : 'none'
}

function authHeaders(): Record<string, string> {
  const initData = getTelegramInitData()
  if (initData) {
    return { 'X-Telegram-Init-Data': initData }
  }
  if (import.meta.env.DEV && import.meta.env.VITE_DEV_TELEGRAM_ID) {
    return {
      'X-Dev-Telegram-Id': import.meta.env.VITE_DEV_TELEGRAM_ID,
      'X-Dev-Username': import.meta.env.VITE_DEV_USERNAME || '',
      'X-Dev-Full-Name': import.meta.env.VITE_DEV_FULL_NAME || ''
    }
  }
  return {}
}

async function request<T>(url: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(),
      ...(options.headers || {})
    }
  })
  if (!response.ok) {
    const text = await response.text()
    let detail = text
    try {
      const payload = JSON.parse(text)
      detail = payload.detail || text
    } catch {
      detail = text
    }
    throw new ApiError(response.status, detail)
  }
  return response.json()
}

async function requestForm<T>(url: string, form: FormData): Promise<T> {
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      ...authHeaders()
    },
    body: form
  })
  if (!response.ok) {
    const text = await response.text()
    let detail = text
    try {
      const payload = JSON.parse(text)
      detail = payload.detail || text
    } catch {
      detail = text
    }
    throw new ApiError(response.status, detail)
  }
  return response.json()
}

function importForm(file: File | null, mode: ImportMode, previewId?: string, confirmForce = false) {
  const form = new FormData()
  form.append('mode', mode)
  if (previewId) form.append('preview_id', previewId)
  if (confirmForce) form.append('confirm_force', 'true')
  if (file) form.append('file', file)
  return form
}

function queryString(query: Record<string, unknown>) {
  const params = new URLSearchParams()
  Object.entries(query).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return
    params.set(key, String(value))
  })
  const text = params.toString()
  return text ? `?${text}` : ''
}

export const api = {
  me: () => request<AppUser>('/api/me'),
  capabilities: () => request<Capabilities>('/api/capabilities'),
  dashboardSummary: () => request<DashboardSummary>('/api/dashboard/summary'),
  pprList: (query: PprListQuery) => request<PprListResponse>(`/api/ppr${queryString(query as Record<string, unknown>)}`),
  users: () => request<AppUser[]>('/api/users'),
  createUser: (payload: UserCreatePayload) => request<AppUser>('/api/users', {
    method: 'POST',
    body: JSON.stringify(payload)
  }),
  updateUser: (id: number, payload: UserUpdatePayload) => request<AppUser>(`/api/users/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(payload)
  }),
  today: () => request<PprNotification[]>('/api/ppr/today'),
  all: (includeArchived = false) => request<PprCard[]>(`/api/ppr/all${includeArchived ? '?include_archived=true' : ''}`),
  unverified: () => request<PprNotification[]>('/api/ppr/unverified'),
  missingDate: () => request<PprCard[]>('/api/ppr/missing-date'),
  ppr: (id: number) => request<PprCard>(`/api/ppr/${id}`),
  createPpr: (payload: Record<string, unknown>) => request<PprCard>('/api/ppr', {
    method: 'POST',
    body: JSON.stringify(payload)
  }),
  updatePpr: (id: number, payload: Record<string, unknown>) => request<PprCard>(`/api/ppr/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(payload)
  }),
  archivePpr: (id: number, force = false) => request<PprCard>(`/api/ppr/${id}/archive${force ? '?force=true' : ''}`, { method: 'POST' }),
  restorePpr: (id: number) => request<PprCard>(`/api/ppr/${id}/restore`, { method: 'POST' }),
  card: (id: number) => request<PprNotification>(`/api/notifications/${id}`),
  take: (id: number) => request<PprNotification>(`/api/notifications/${id}/take`, { method: 'POST' }),
  check: (id: number) => request<PprNotification>(`/api/notifications/${id}/check`, { method: 'POST' }),
  notifications: (query: Record<string, unknown>) => request<NotificationListResponse>(`/api/notifications${queryString(query)}`),
  retryNotification: (id: number) => request<PprNotification>(`/api/notifications/${id}/retry`, { method: 'POST' }),
  markNotificationSent: (id: number, comment: string) => request<PprNotification>(`/api/notifications/${id}/mark-sent`, {
    method: 'POST',
    body: JSON.stringify({ comment })
  }),
  retryUnknownNotification: (id: number) => request<PprNotification>(`/api/notifications/${id}/retry-unknown?confirm=true`, { method: 'POST' }),
  comment: (id: number, comment: string) => request<{ ok: boolean }>(`/api/notifications/${id}/comment`, {
    method: 'POST',
    body: JSON.stringify({ comment })
  }),
  importPreview: (file: File | null, mode: ImportMode) => requestForm<ImportPreviewResponse>(
    '/api/import/excel/preview',
    importForm(file, mode)
  ),
  importApply: (file: File | null, mode: ImportMode, previewId: string, confirmForce: boolean) => requestForm<ImportPreviewResponse>(
    '/api/import/excel/apply',
    importForm(file, mode, previewId, confirmForce)
  )
}
