export type NotificationStatus = 'planned' | 'processing' | 'delivery_unknown' | 'sent' | 'failed' | 'skipped' | 'cancelled'
export type PprCardStatus = 'scheduled' | 'in_progress' | 'verified' | 'archived' | 'cancelled'
export type UserRole = 'admin' | 'checker'
export type PprSort = 'date_asc' | 'date_desc' | 'overdue_first' | 'updated_desc' | 'title' | 'project'

export interface AppUser {
  id: number
  telegram_id: string
  username?: string | null
  full_name?: string | null
  role: UserRole
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface Capabilities {
  features: {
    outlook: boolean
  }
}

export interface UserCreatePayload {
  telegram_id: string
  username?: string | null
  full_name?: string | null
  role: UserRole
  is_active: boolean
}

export interface UserUpdatePayload {
  username?: string | null
  full_name?: string | null
  role?: UserRole
  is_active?: boolean
}

export interface PprCard {
  notification_id: number | null
  type?: 'start' | 'end' | null
  scheduled_at?: string | null
  status: PprCardStatus
  notification_status?: NotificationStatus | null
  attempt_count?: number
  last_attempt_at?: string | null
  last_error?: string | null
  processing_started_at?: string | null
  processing_by?: string | null
  processing_phase?: string | null
  telegram_chat_id?: string | null
  telegram_message_id?: string | null
  sent_at?: string | null
  requires_date: boolean
  is_archived: boolean
  taken_by_name?: string | null
  taken_at?: string | null
  checked_by_name?: string | null
  checked_at?: string | null
  event: {
    id: number
    external_id: string
    source_key?: string | null
    date?: string | null
    start_time?: string | null
    end_time?: string | null
    project?: string | null
    title: string
    activities: string[]
    responsible_setup?: string | null
    responsible_report?: string | null
    source_link?: string | null
    outlook_link?: string | null
    outlook_url?: string | null
    notify_start?: boolean
    notify?: boolean
    ppr_status?: PprCardStatus
    is_active?: boolean
    is_archived?: boolean
    is_manually_edited?: boolean
    manual_updated_at?: string | null
    comment?: string | null
  }
  audit_log: Array<{
    action: string
    user_name?: string | null
    comment?: string | null
    created_at: string
  }>
}

export type PprNotification = PprCard & {
  notification_id: number
  type: 'start' | 'end'
  scheduled_at: string
  status: PprCardStatus
  notification_status: NotificationStatus
  requires_date: false
}

export interface DashboardSummary {
  today: number
  scheduled: number
  in_progress: number
  verified: number
  overdue: number
  missing_date: number
  notification_errors: number
  archived: number
}

export interface PprListQuery {
  search?: string
  status?: string
  project?: string
  date_from?: string
  date_to?: string
  date_state?: 'present' | 'missing' | ''
  checker?: string
  notify?: boolean | null
  outlook?: boolean | null
  include_archived?: boolean
  sort?: PprSort
  page?: number
  page_size?: number
  quick_filter?: string
}

export interface PprListResponse {
  page: number
  page_size: number
  total: number
  items: PprCard[]
}

export interface NotificationListResponse {
  page: number
  page_size: number
  total: number
  items: PprNotification[]
}

export type ImportMode = 'safe' | 'new_only' | 'force'

export interface ImportPreviewDetail {
  excel_row_number?: number | null
  external_id?: string | null
  source_key?: string | null
  title?: string | null
  project?: string | null
  current_date?: string | null
  current_time?: string | null
  new_date?: string | null
  new_time?: string | null
  action: 'create' | 'update' | 'unchanged' | 'skip_manual' | 'duplicate' | 'invalid' | 'missing_from_excel' | 'source_key_migration' | 'ambiguous'
  key_method?: 'id' | 'source' | 'fingerprint' | 'none'
  match_method?: 'source_key' | 'external_id' | 'legacy_row' | 'fingerprint' | 'ambiguous' | 'none'
  confidence?: string
  reason: string
  fields_changed: Record<string, { old: string | number | boolean | null; new: string | number | boolean | null }>
  notification_action?: string
  notification_reason?: string
  ppr_event_id?: number | null
}

export interface ImportPreviewSummary {
  total_rows: number
  valid_rows: number
  invalid_rows: number
  new_events: number
  updated_events: number
  unchanged_events: number
  skipped_manual_events: number
  duplicate_rows: number
  missing_date_rows: number
  notifications_to_create: number
  notifications_to_update: number
  notifications_to_skip: number
  events_missing_from_excel: number
  errors_count: number
  match_by_method?: Record<string, number> | null
}

export interface ImportPreviewResponse {
  preview_id: string
  filename: string
  mode: ImportMode
  file_hash: string
  summary: ImportPreviewSummary
  details: ImportPreviewDetail[]
  warnings: string[]
  applied?: {
    created: number
    updated: number
    unchanged: number
    skipped: number
    notifications_synced: number
  }
}
