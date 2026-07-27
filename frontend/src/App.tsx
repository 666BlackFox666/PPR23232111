import { useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, api, getAuthMode } from './api'
import type { ReactNode } from 'react'
import type { AppUser, DashboardSummary, ImportMode, ImportPreviewDetail, ImportPreviewResponse, PprCard, PprListQuery, PprNotification, PprSort, UserRole } from './types'

type TabKey = 'today' | 'all' | 'unverified' | 'missingDate' | 'users' | 'importExcel' | 'deliveryErrors'

const tabs: Array<{ key: TabKey; label: string }> = [
  { key: 'today', label: 'Сегодня' },
  { key: 'all', label: 'Все' },
  { key: 'unverified', label: 'Непроверенные' },
  { key: 'missingDate', label: 'Без даты' }
]

const adminTabs: Array<{ key: TabKey; label: string }> = [
  ...tabs,
  { key: 'deliveryErrors', label: 'Ошибки' },
  { key: 'importExcel', label: 'Импорт' },
  { key: 'users', label: 'Пользователи' }
]

const statusMap: Record<string, string> = {
  planned: 'Запланировано',
  processing: 'Отправляется',
  scheduled: 'Ожидает взятия',
  sent: 'Отправлено',
  in_progress: 'В работе',
  delivery_unknown: 'Неизвестно',
  verified: 'Проверено',
  error: 'Ошибка',
  failed: 'Ошибка',
  skipped: 'Пропущено',
  cancelled: 'Отменено',
  missing_date: 'Нет даты',
  no_notification: 'Без уведомления',
  archived: 'Архив'
}

type FiltersState = {
  search: string
  status: string
  project: string
  date_from: string
  date_to: string
  date_state: '' | 'present' | 'missing'
  checker: string
  notify: '' | 'true' | 'false'
  outlook: '' | 'true' | 'false'
  quick_filter: string
}

const pageSize = 25
const moscowTimeZone = 'Europe/Moscow'
const timestampWithOffset = /(Z|[+-]\d{2}:?\d{2})$/i

function emptyFilters(): FiltersState {
  return {
    search: '',
    status: '',
    project: '',
    date_from: '',
    date_to: '',
    date_state: '',
    checker: '',
    notify: '',
    outlook: '',
    quick_filter: 'today'
  }
}

const dashboardLabels: Array<{ key: keyof DashboardSummary; label: string; quick?: string; status?: string; adminOnly?: boolean }> = [
  { key: 'today', label: 'Сегодня', quick: 'today' },
  { key: 'scheduled', label: 'Ожидают взятия', status: 'scheduled' },
  { key: 'in_progress', label: 'В работе', status: 'in_progress' },
  { key: 'verified', label: 'Проверены', status: 'verified' },
  { key: 'overdue', label: 'Просрочены', quick: 'overdue' },
  { key: 'missing_date', label: 'Без даты', quick: 'missing_date' },
  { key: 'notification_errors', label: 'Ошибки отправки', quick: 'notification_errors' },
  { key: 'archived', label: 'Архив', status: 'archived', adminOnly: true }
]

function boolFilter(value: FiltersState['notify']) {
  if (value === 'true') return true
  if (value === 'false') return false
  return null
}

const actionMap: Record<string, string> = {
  take: 'Взял в работу',
  taken: 'Взял в работу',
  verify: 'Проверено',
  checked: 'Проверено',
  forced_archive: 'Принудительная архивация',
  restored: 'Восстановлено',
  restore: 'Восстановлено',
  comment: 'Комментарий'
}

function formatDate(value?: string | null) {
  if (!value) return 'Не заполнена'
  return new Date(`${value}T00:00:00`).toLocaleDateString('ru-RU')
}

function formatTime(value?: string | null) {
  if (!value) return 'Не заполнено'
  return value.slice(0, 5)
}

function formatDateTime(value?: string | null) {
  if (!value) return ''
  const timestamp = timestampWithOffset.test(value) ? value : `${value}Z`
  const date = new Date(timestamp)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString('ru-RU', { timeZone: moscowTimeZone })
}

function formatScheduledDateTime(value?: string | null) {
  if (!value) return ''
  if (timestampWithOffset.test(value)) return formatDateTime(value)

  // scheduled_at is a Moscow wall-clock value from the PPR schedule, not a UTC audit timestamp.
  const match = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?$/.exec(value)
  if (!match) return ''
  const [, year, month, day, hours, minutes, seconds] = match
  return `${day}.${month}.${year}, ${hours}:${minutes}${seconds ? `:${seconds}` : ''}`
}

function parseNotificationStartParam(value?: string | null) {
  const match = /^notification_(\d+)$/.exec(value || '')
  return match ? match[1] : null
}

function getStartNotificationId() {
  const params = new URLSearchParams(location.search)
  const telegramStartParam =
    window.Telegram?.WebApp?.initDataUnsafe?.start_param ||
    params.get('tgWebAppStartParam')
  const fromTelegram = parseNotificationStartParam(telegramStartParam)
  if (fromTelegram) return fromTelegram

  if (getAuthMode() === 'dev') {
    return params.get('notification_id')
  }
  return null
}

function StatusBadge({ status }: { status: string }) {
  return <span className={`badge badge-${status}`}>{statusMap[status] || status}</span>
}

function getDisplayedStatus(item: Pick<PprCard, 'is_archived' | 'requires_date' | 'notification_id' | 'status'>) {
  if (item.is_archived) return 'archived'
  if (item.requires_date) return 'missing_date'
  if (item.notification_id == null) return 'no_notification'
  return item.status
}

function InfoRow({ label, value }: { label: string; value?: string | null }) {
  return (
    <div className="info-row">
      <span>{label}</span>
      <b>{value || 'Не заполнено'}</b>
    </div>
  )
}

function Notice({ children, tone = 'info' }: { children: ReactNode; tone?: 'info' | 'warning' | 'error' }) {
  return <div className={`notice notice-${tone}`}>{children}</div>
}

type PprFormState = {
  title: string
  project: string
  date: string
  start_time: string
  activities: string
  notify: boolean
  outlook_link: string
  comment: string
}

function emptyForm(): PprFormState {
  return {
    title: '',
    project: '',
    date: '',
    start_time: '08:00',
    activities: '',
    notify: true,
    outlook_link: '',
    comment: ''
  }
}

function formFromCard(item: PprCard): PprFormState {
  return {
    title: item.event.title || '',
    project: item.event.project || '',
    date: item.event.date || '',
    start_time: formatTime(item.event.start_time) === 'Не заполнено' ? '' : formatTime(item.event.start_time),
    activities: item.event.activities.join('\n'),
    notify: item.event.notify ?? item.event.notify_start ?? true,
    outlook_link: item.event.outlook_url || item.event.outlook_link || '',
    comment: item.event.comment || ''
  }
}

function formPayload(form: PprFormState) {
  return {
    title: form.title.trim(),
    project: form.project.trim() || null,
    date: form.date || null,
    start_time: form.start_time || null,
    activities: form.activities.trim() || null,
    notify: form.notify,
    outlook_link: form.outlook_link.trim() || null,
    comment: form.comment.trim() || null
  }
}

function ListItem({ item, onOpen }: { item: PprCard; onOpen: (item: PprCard) => void }) {
  return (
    <button className="card list-item" onClick={() => onOpen(item)}>
      <div className="item-top">
        <div className="item-time">
          <span>{formatDate(item.event.date)}</span>
          <b>{formatTime(item.event.start_time)}</b>
        </div>
        <div className="badge-group">
          <StatusBadge status={getDisplayedStatus(item)} />
        </div>
      </div>
      <div className="item-title">{item.event.title}</div>
      <div className="item-meta">
        {item.event.project && <span>{item.event.project}</span>}
        {item.taken_by_name && <span>В работе: {item.taken_by_name}</span>}
        {item.checked_by_name && <span>Проверил: {item.checked_by_name}</span>}
      </div>
    </button>
  )
}

function EmptyState({ tab }: { tab: TabKey }) {
  const message =
    tab === 'today'
      ? 'На сегодня ППР не запланированы.'
      : tab === 'missingDate'
        ? 'Нет ППР без даты.'
        : tab === 'unverified'
            ? 'Нет ППР, ожидающих проверки.'
            : tab === 'importExcel'
              ? 'Выберите Excel-файл и сначала выполните предпросмотр.'
          : tab === 'users'
            ? 'Пользователей пока нет.'
            : 'Список ППР пуст.'

  return <div className="empty-state">{message}</div>
}

type UserFormState = {
  telegram_id: string
  username: string
  full_name: string
  role: UserRole
  is_active: boolean
}

function emptyUserForm(): UserFormState {
  return {
    telegram_id: '',
    username: '',
    full_name: '',
    role: 'checker',
    is_active: true
  }
}

function userFormFromUser(user: AppUser): UserFormState {
  return {
    telegram_id: user.telegram_id,
    username: user.username || '',
    full_name: user.full_name || '',
    role: user.role,
    is_active: user.is_active
  }
}

function cleanOptional(value: string) {
  const trimmed = value.trim()
  return trimmed || null
}

function userErrorMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 403) {
    return 'Нет доступа. Обратитесь к администратору.'
  }
  if (error instanceof Error) {
    return error.message
  }
  return 'Операция не выполнена.'
}

function UserForm({
  mode,
  form,
  saving,
  onChange,
  onSubmit,
  onCancel
}: {
  mode: 'create' | 'edit'
  form: UserFormState
  saving: boolean
  onChange: (form: UserFormState) => void
  onSubmit: () => void
  onCancel: () => void
}) {
  function setField<K extends keyof UserFormState>(key: K, value: UserFormState[K]) {
    onChange({ ...form, [key]: value })
  }

  return (
    <div className="user-form">
      {mode === 'create' && (
        <label>
          <span>Telegram ID</span>
          <input value={form.telegram_id} onChange={event => setField('telegram_id', event.target.value)} />
        </label>
      )}
      <label>
        <span>Username</span>
        <input value={form.username} onChange={event => setField('username', event.target.value)} placeholder="username без @" />
      </label>
      <label>
        <span>Full name</span>
        <input value={form.full_name} onChange={event => setField('full_name', event.target.value)} />
      </label>
      <div className="form-row">
        <label>
          <span>Role</span>
          <select value={form.role} onChange={event => setField('role', event.target.value as UserRole)}>
            <option value="checker">checker</option>
            <option value="admin">admin</option>
          </select>
        </label>
        <label className="checkbox-row user-active-toggle">
          <input type="checkbox" checked={form.is_active} onChange={event => setField('is_active', event.target.checked)} />
          <span>Пользователь активен</span>
        </label>
      </div>
      <div className="actions form-actions">
        <button className="primary-button" onClick={onSubmit} disabled={saving}>
          {mode === 'create' ? 'Добавить' : 'Сохранить'}
        </button>
        <button className="secondary-button" onClick={onCancel} disabled={saving}>Отмена</button>
      </div>
    </div>
  )
}

function UserManagementView({ onCountChange }: { onCountChange: (count: number) => void }) {
  const [users, setUsers] = useState<AppUser[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [createForm, setCreateForm] = useState<UserFormState>(() => emptyUserForm())
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editForm, setEditForm] = useState<UserFormState>(() => emptyUserForm())

  async function loadUsers() {
    setLoading(true)
    setError('')
    try {
      const data = await api.users()
      setUsers(data)
      onCountChange(data.length)
    } catch (e) {
      setUsers([])
      onCountChange(0)
      setError(userErrorMessage(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadUsers()
  }, [])

  async function submitCreate() {
    setError('')
    const telegramId = createForm.telegram_id.trim()
    if (!telegramId) {
      setError('Telegram ID обязателен.')
      return
    }
    setSaving(true)
    try {
      await api.createUser({
        telegram_id: telegramId,
        username: cleanOptional(createForm.username),
        full_name: cleanOptional(createForm.full_name),
        role: createForm.role,
        is_active: createForm.is_active
      })
      setCreateForm(emptyUserForm())
      setCreateOpen(false)
      await loadUsers()
    } catch (e) {
      setError(userErrorMessage(e))
    } finally {
      setSaving(false)
    }
  }

  function startEdit(user: AppUser) {
    setError('')
    setEditingId(user.id)
    setEditForm(userFormFromUser(user))
  }

  async function submitEdit(user: AppUser) {
    setError('')
    setSaving(true)
    try {
      await api.updateUser(user.id, {
        username: editForm.username.trim(),
        full_name: editForm.full_name.trim(),
        role: editForm.role,
        is_active: editForm.is_active
      })
      setEditingId(null)
      await loadUsers()
    } catch (e) {
      setError(userErrorMessage(e))
    } finally {
      setSaving(false)
    }
  }

  async function toggleActive(user: AppUser) {
    setError('')
    setSaving(true)
    try {
      await api.updateUser(user.id, { is_active: !user.is_active })
      await loadUsers()
    } catch (e) {
      setError(userErrorMessage(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="users-section">
      <div className="admin-toolbar">
        <button className="primary-button" onClick={() => setCreateOpen(value => !value)}>
          {createOpen ? 'Скрыть форму' : 'Добавить пользователя'}
        </button>
      </div>

      {error && <Notice tone="error">{error}</Notice>}

      {createOpen && (
        <article className="card users-form-card">
          <h2>Добавить пользователя</h2>
          <UserForm
            mode="create"
            form={createForm}
            saving={saving}
            onChange={setCreateForm}
            onSubmit={submitCreate}
            onCancel={() => setCreateOpen(false)}
          />
        </article>
      )}

      {loading && <div className="loading">Загрузка...</div>}
      {!loading && users.length === 0 && !error && <EmptyState tab="users" />}

      <div className="users-list">
        {users.map(user => (
          <article key={user.id} className={`card user-card ${user.is_active ? '' : 'user-card-inactive'}`}>
            <div className="user-card-head">
              <div>
                <h2>{user.full_name || user.username || user.telegram_id}</h2>
                <p>{user.username ? `@${user.username}` : 'username не заполнен'}</p>
              </div>
              <div className="badge-group">
                <span className={`badge ${user.role === 'admin' ? 'badge-checked' : 'badge-planned'}`}>{user.role}</span>
                <span className={`badge ${user.is_active ? 'badge-checked' : 'badge-archived'}`}>
                  {user.is_active ? 'Активен' : 'Отключен'}
                </span>
              </div>
            </div>

            <div className="user-fields">
              <InfoRow label="Telegram ID" value={user.telegram_id} />
              <InfoRow label="Username" value={user.username ? `@${user.username}` : null} />
              <InfoRow label="Full name" value={user.full_name} />
              <InfoRow label="Создан" value={formatDateTime(user.created_at)} />
              <InfoRow label="Обновлен" value={formatDateTime(user.updated_at)} />
            </div>

            {editingId === user.id ? (
              <UserForm
                mode="edit"
                form={editForm}
                saving={saving}
                onChange={setEditForm}
                onSubmit={() => submitEdit(user)}
                onCancel={() => setEditingId(null)}
              />
            ) : (
              <div className="actions user-actions">
                <button className="secondary-button" onClick={() => startEdit(user)} disabled={saving}>Редактировать</button>
                <button className="secondary-button" onClick={() => toggleActive(user)} disabled={saving}>
                  {user.is_active ? 'Отключить' : 'Включить'}
                </button>
              </div>
            )}
          </article>
        ))}
      </div>
    </section>
  )
}

function DeliveryErrorsView({ onCountChange }: { onCountChange: (count: number) => void }) {
  const [items, setItems] = useState<PprNotification[]>([])
  const [unknownItems, setUnknownItems] = useState<PprNotification[]>([])
  const [total, setTotal] = useState(0)
  const [unknownTotal, setUnknownTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const pageSize = 25

  async function load(nextPage = 1, append = false) {
    setLoading(true)
    setError('')
    try {
      const [data, unknown] = await Promise.all([
        api.notifications({ failed_only: true, page: nextPage, page_size: pageSize }),
        api.notifications({ status: 'delivery_unknown', page: 1, page_size: 100 })
      ])
      setItems(current => append ? [...current, ...data.items] : data.items)
      setUnknownItems(unknown.items)
      setTotal(data.total)
      setUnknownTotal(unknown.total)
      setPage(data.page)
      onCountChange(data.total + unknown.total)
    } catch (e) {
      setItems([])
      setUnknownItems([])
      setTotal(0)
      setUnknownTotal(0)
      onCountChange(0)
      setError(userErrorMessage(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  async function retry(item: PprNotification) {
    setError('')
    setMessage('')
    try {
      await api.retryNotification(item.notification_id)
      setMessage(`Уведомление #${item.notification_id} возвращено в planned.`)
      await load(1, false)
    } catch (e) {
      setError(userErrorMessage(e))
    }
  }

  async function markSent(item: PprNotification) {
    const comment = window.prompt('Комментарий обязателен. Например: сообщение найдено в Telegram-чате.')
    if (!comment?.trim()) return
    setError('')
    setMessage('')
    try {
      await api.markNotificationSent(item.notification_id, comment.trim())
      setMessage(`Уведомление #${item.notification_id} отмечено как sent.`)
      await load(1, false)
    } catch (e) {
      setError(userErrorMessage(e))
    }
  }

  async function retryUnknown(item: PprNotification) {
    if (!window.confirm('Telegram мог уже получить сообщение. Повторная отправка может создать дубль. Продолжить?')) return
    setError('')
    setMessage('')
    try {
      await api.retryUnknownNotification(item.notification_id)
      setMessage(`Уведомление #${item.notification_id} возвращено в planned. Возможен дубль при повторной отправке.`)
      await load(1, false)
    } catch (e) {
      setError(userErrorMessage(e))
    }
  }

  return (
    <section className="delivery-errors-section">
      {message && <Notice>{message}</Notice>}
      {error && <Notice tone="error">{error}</Notice>}
      {loading && <div className="loading">Загрузка...</div>}
      {!loading && items.length === 0 && unknownItems.length === 0 && !error && <div className="empty-state">Ошибок отправки нет.</div>}
      {unknownItems.length > 0 && (
        <>
          <h2 className="section-title">Неизвестен результат отправки</h2>
          <div className="list">
            {unknownItems.map(item => (
              <article key={item.notification_id} className="card delivery-error-card">
                <div className="item-top">
                  <div>
                    <h2>{item.event.title}</h2>
                    <p>{item.event.project || 'Проект не заполнен'}</p>
                  </div>
                  <StatusBadge status={item.notification_status || 'delivery_unknown'} />
                </div>
                <div className="user-fields">
                  <InfoRow label="Уведомление" value={`#${item.notification_id}`} />
                  <InfoRow label="Запланировано" value={formatScheduledDateTime(item.scheduled_at)} />
                  <InfoRow label="Processing started" value={formatDateTime(item.processing_started_at)} />
                  <InfoRow label="Processing by" value={item.processing_by || 'Не заполнено'} />
                  <InfoRow label="Ошибка" value={item.last_error || 'Не заполнена'} />
                </div>
                <div className="actions">
                  <button className="secondary-button" onClick={() => markSent(item)} disabled={loading}>Сообщение уже отправлено</button>
                  <button className="primary-button" onClick={() => retryUnknown(item)} disabled={loading}>Отправить повторно</button>
                </div>
              </article>
            ))}
          </div>
          {unknownTotal > unknownItems.length && <p className="muted">Показаны первые {unknownItems.length} из {unknownTotal}.</p>}
        </>
      )}
      {items.length > 0 && <h2 className="section-title">Ошибки отправки</h2>}
      <div className="list">
        {items.map(item => (
          <article key={item.notification_id} className="card delivery-error-card">
            <div className="item-top">
              <div>
                <h2>{item.event.title}</h2>
                <p>{item.event.project || 'Проект не заполнен'}</p>
              </div>
              <StatusBadge status={item.notification_status || 'failed'} />
            </div>
            <div className="user-fields">
              <InfoRow label="Уведомление" value={`#${item.notification_id}`} />
              <InfoRow label="ППР" value={`#${item.event.id}`} />
              <InfoRow label="Запланировано" value={formatScheduledDateTime(item.scheduled_at)} />
              <InfoRow label="Попытки" value={String(item.attempt_count ?? 0)} />
              <InfoRow label="Последняя попытка" value={formatDateTime(item.last_attempt_at)} />
              <InfoRow label="Ошибка" value={item.last_error || 'Не заполнена'} />
            </div>
            <div className="actions">
              <button className="primary-button" onClick={() => retry(item)} disabled={loading}>Повторить отправку</button>
            </div>
          </article>
        ))}
      </div>
      {items.length < total && (
        <button className="load-more-button" onClick={() => load(page + 1, true)} disabled={loading}>
          Показать еще
        </button>
      )}
    </section>
  )
}

type ImportFilter = 'all' | 'new' | 'changed' | 'skipped' | 'errors' | 'duplicates'

const importFilterLabels: Record<ImportFilter, string> = {
  all: 'Все',
  new: 'Новые',
  changed: 'Изменяемые',
  skipped: 'Пропущенные',
  errors: 'Ошибки',
  duplicates: 'Дубли'
}

function importActionLabel(action: ImportPreviewDetail['action']) {
  const labels: Record<ImportPreviewDetail['action'], string> = {
    create: 'Создать',
    update: 'Обновить',
    unchanged: 'Без изменений',
    skip_manual: 'Ручная правка',
    duplicate: 'Дубль',
    invalid: 'Ошибка',
    missing_from_excel: 'Нет в Excel',
    source_key_migration: 'Миграция ключа',
    ambiguous: 'Конфликт'
  }
  return labels[action] || action
}

function detailMatchesFilter(item: ImportPreviewDetail, filter: ImportFilter) {
  if (filter === 'all') return true
  if (filter === 'new') return item.action === 'create'
  if (filter === 'changed') return item.action === 'update'
  if (filter === 'skipped') return ['unchanged', 'skip_manual', 'missing_from_excel', 'source_key_migration'].includes(item.action)
  if (filter === 'errors') return ['invalid', 'ambiguous'].includes(item.action)
  if (filter === 'duplicates') return item.action === 'duplicate'
  return true
}

function changedFieldsText(item: ImportPreviewDetail) {
  const entries = Object.entries(item.fields_changed || {})
  if (entries.length === 0) return 'Нет'
  return entries.map(([field, value]) => `${field}: ${value.old ?? 'пусто'} -> ${value.new ?? 'пусто'}`).join('; ')
}

function ImportExcelView() {
  const [file, setFile] = useState<File | null>(null)
  const [mode, setMode] = useState<ImportMode>('safe')
  const [preview, setPreview] = useState<ImportPreviewResponse | null>(null)
  const [filter, setFilter] = useState<ImportFilter>('all')
  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const filteredDetails = useMemo(
    () => (preview?.details || []).filter(item => detailMatchesFilter(item, filter)),
    [preview, filter]
  )
  const canApply = Boolean(preview?.preview_id && preview.summary.errors_count === 0)

  async function runPreview() {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const result = await api.importPreview(file, mode)
      setPreview(result)
      setFilter('all')
      if (result.summary.errors_count > 0) {
        setMessage('Предпросмотр готов, но есть ошибки или дубли. Исправьте их перед применением.')
      } else {
        setMessage('Предпросмотр готов. Можно применить импорт.')
      }
    } catch (e: any) {
      setPreview(null)
      setError(e.message || 'Предпросмотр не выполнен.')
    } finally {
      setLoading(false)
    }
  }

  async function applyImport() {
    if (!preview?.preview_id) return
    if (mode === 'force' && !window.confirm('Force-импорт перезапишет ручные изменения. Продолжить?')) return
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const result = await api.importApply(file, mode, preview.preview_id, mode === 'force')
      setPreview(result)
      setMessage(`Импорт применен: создано ${result.applied?.created ?? 0}, обновлено ${result.applied?.updated ?? 0}, уведомлений синхронизировано ${result.applied?.notifications_synced ?? 0}.`)
    } catch (e: any) {
      setError(e.message || 'Импорт не применен.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="import-section">
      <article className="card import-panel">
        <h2>Импорт Excel</h2>
        <div className="form-grid">
          <label>
            <span>Excel-файл</span>
            <input
              type="file"
              accept=".xlsx,.xlsm"
              onChange={event => {
                setFile(event.target.files?.[0] || null)
                setPreview(null)
                setMessage('')
                setError('')
              }}
            />
          </label>
          <label>
            <span>Режим</span>
            <select
              value={mode}
              onChange={event => {
                setMode(event.target.value as ImportMode)
                setPreview(null)
              }}
            >
              <option value="safe">safe - не перетирать ручные правки</option>
              <option value="new_only">new_only - только новые ППР</option>
              <option value="force">force - перезаписать ручные правки</option>
            </select>
          </label>
        </div>
        <Notice>Сначала выполните предпросмотр. Применение разрешено только для того же файла и режима.</Notice>
        {mode === 'force' && <Notice tone="warning">Force требует отдельного подтверждения перед применением.</Notice>}
        <div className="actions form-actions">
          <button className="primary-button" onClick={runPreview} disabled={loading}>
            Предпросмотр
          </button>
          {canApply && (
            <button className="secondary-button" onClick={applyImport} disabled={loading}>
              Применить импорт
            </button>
          )}
        </div>
        {message && <Notice>{message}</Notice>}
        {error && <Notice tone="error">{error}</Notice>}
      </article>

      {preview && (
        <>
          {preview.warnings.map((warning, index) => <Notice key={index} tone="warning">{warning}</Notice>)}
          <article className="card import-summary">
            <h2>Summary</h2>
            <div className="summary-grid">
              {Object.entries(preview.summary).map(([key, value]) => (
                <div key={key} className="summary-item">
                  <span>{key}</span>
                  <b>{value}</b>
                </div>
              ))}
            </div>
          </article>

          <div className="import-filters">
            {(Object.keys(importFilterLabels) as ImportFilter[]).map(item => (
              <button key={item} className={filter === item ? 'active' : ''} onClick={() => setFilter(item)}>
                {importFilterLabels[item]}
              </button>
            ))}
          </div>

          <div className="import-details">
            {filteredDetails.map((item, index) => (
              <article key={`${item.action}-${item.excel_row_number ?? item.ppr_event_id ?? index}`} className={`card import-detail import-action-${item.action}`}>
                <div className="import-detail-head">
                  <div>
                    <h3>{item.title || 'Без названия'}</h3>
                    <p>{item.project || 'Проект не заполнен'}</p>
                  </div>
                  <span className={`badge badge-${item.action}`}>{importActionLabel(item.action)}</span>
                </div>
                <div className="user-fields">
                  <InfoRow label="Excel row" value={item.excel_row_number ? String(item.excel_row_number) : null} />
                  <InfoRow label="source_key" value={item.source_key} />
                  <InfoRow label="key_method" value={item.key_method} />
                  <InfoRow label="match_method" value={item.match_method} />
                  <InfoRow label="confidence" value={item.confidence} />
                  <InfoRow label="Дата сейчас" value={item.current_date} />
                  <InfoRow label="Дата новая" value={item.new_date} />
                  <InfoRow label="Время сейчас" value={item.current_time?.slice(0, 5)} />
                  <InfoRow label="Время новое" value={item.new_time?.slice(0, 5)} />
                  <InfoRow label="Notification" value={item.notification_action} />
                </div>
                <p className="muted">{item.reason}</p>
                {item.notification_reason && <p className="muted">{item.notification_reason}</p>}
                <p className="changed-fields">{changedFieldsText(item)}</p>
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  )
}

function PprFormView({
  mode,
  initial,
  onCancel,
  onSaved
}: {
  mode: 'create' | 'edit'
  initial?: PprCard
  onCancel: () => void
  onSaved: (item: PprCard) => void
}) {
  const [form, setForm] = useState<PprFormState>(() => initial ? formFromCard(initial) : emptyForm())
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  function setField<K extends keyof PprFormState>(key: K, value: PprFormState[K]) {
    setForm(current => ({ ...current, [key]: value }))
  }

  async function submit() {
    setError('')
    if (!form.title.trim()) {
      setError('Название ППР обязательно.')
      return
    }
    setSaving(true)
    try {
      const saved = mode === 'create'
        ? await api.createPpr(formPayload(form))
        : await api.updatePpr(initial!.event.id, formPayload(form))
      onSaved(saved)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <main className="app-shell">
      <button className="back-button" onClick={onCancel}>Назад</button>
      <article className="card detail-card">
        <div className="detail-header">
          <h1>{mode === 'create' ? 'Добавить ППР' : 'Редактировать ППР'}</h1>
        </div>
        {error && <Notice tone="error">{error}</Notice>}
        <div className="form-grid">
          <label>
            <span>Название ППР</span>
            <input value={form.title} onChange={event => setField('title', event.target.value)} />
          </label>
          <label>
            <span>Проект</span>
            <input value={form.project} onChange={event => setField('project', event.target.value)} />
          </label>
          <div className="form-row">
            <label>
              <span>Дата выхода</span>
              <input type="date" value={form.date} onChange={event => setField('date', event.target.value)} />
            </label>
            <label>
              <span>Время выхода</span>
              <input type="time" value={form.start_time} onChange={event => setField('start_time', event.target.value)} />
            </label>
          </div>
          <label>
            <span>Активности / описание работ</span>
            <textarea value={form.activities} onChange={event => setField('activities', event.target.value)} />
          </label>
          <label className="checkbox-row">
            <input type="checkbox" checked={form.notify} onChange={event => setField('notify', event.target.checked)} />
            <span>Отправлять уведомление</span>
          </label>
          <label>
            <span>Outlook-ссылка</span>
            <input value={form.outlook_link} onChange={event => setField('outlook_link', event.target.value)} />
          </label>
          <label>
            <span>Комментарий</span>
            <textarea value={form.comment} onChange={event => setField('comment', event.target.value)} />
          </label>
        </div>
        <div className="actions form-actions">
          <button className="primary-button" onClick={submit} disabled={saving}>Сохранить</button>
          <button className="secondary-button" onClick={onCancel} disabled={saving}>Отмена</button>
        </div>
      </article>
    </main>
  )
}

function CardView({
  item,
  currentUser,
  onEdit,
  onArchive,
  onRestore,
  onBack,
  onChanged
}: {
  item: PprCard
  currentUser: AppUser
  onEdit: (item: PprCard) => void
  onArchive: (item: PprCard) => void
  onRestore: (item: PprCard) => void
  onBack: () => void
  onChanged: (item: PprCard) => void
}) {
  const [error, setError] = useState('')
  const [comment, setComment] = useState('')
  const [commentOpen, setCommentOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const notificationId = item.notification_id
  const hasNotification = notificationId !== null
  const outlookUrl = item.event.outlook_url || item.event.outlook_link
  const isAdmin = currentUser.role === 'admin'

  async function action(kind: 'take' | 'check') {
    if (notificationId === null) return
    setError('')
    setSaving(true)
    try {
      const updated = kind === 'take' ? await api.take(notificationId) : await api.check(notificationId)
      onChanged(updated)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  async function saveComment() {
    if (notificationId === null || !comment.trim()) return
    setError('')
    setSaving(true)
    try {
      await api.comment(notificationId, comment.trim())
      setComment('')
      setCommentOpen(false)
      const fresh = await api.card(notificationId)
      onChanged(fresh)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <main className="app-shell">
      <button className="back-button" onClick={onBack}>Назад</button>
      <article className="card detail-card">
        <div className="detail-header">
          <h1>{item.event.title}</h1>
          <div className="badge-group">
            <StatusBadge status={getDisplayedStatus(item)} />
          </div>
        </div>

        {item.requires_date && (
          <Notice tone="warning">Нет даты - уведомление не будет создано.</Notice>
        )}

        {error && <Notice tone="error">{error}</Notice>}

        <section className="info-grid">
          <InfoRow label="Проект" value={item.event.project} />
          <InfoRow label="Дата" value={formatDate(item.event.date)} />
          <InfoRow label="Время выхода" value={formatTime(item.event.start_time)} />
          <InfoRow label="Ответственный за настройку" value={item.event.responsible_setup} />
          <InfoRow label="Ответственный за отчетку" value={item.event.responsible_report} />
          {item.taken_by_name && <InfoRow label="Кто взял в работу" value={item.taken_by_name} />}
          {item.checked_by_name && <InfoRow label="Кто проверил" value={item.checked_by_name} />}
        </section>

        {outlookUrl && (
          <section className="section">
            <a className="external-link" href={outlookUrl} target="_blank" rel="noreferrer">Outlook: открыть событие</a>
          </section>
        )}

        {isAdmin && (
          <section className="section">
            <div className="actions">
              <button className="secondary-button" onClick={() => onEdit(item)}>Редактировать</button>
              {!item.is_archived && <button className="secondary-button" onClick={() => onArchive(item)}>Отключить ППР</button>}
              {item.is_archived && <button className="secondary-button" onClick={() => onRestore(item)}>Восстановить</button>}
            </div>
          </section>
        )}

        {item.event.activities.length > 0 && (
          <section className="section">
            <h2>Активности</h2>
            <ol className="activity-list">
              {item.event.activities.map((activity, index) => <li key={index}>{activity}</li>)}
            </ol>
          </section>
        )}

        {hasNotification && !item.is_archived && !['skipped', 'cancelled'].includes(item.notification_status || '') && (
          <section className="section">
            <div className="actions">
              {item.status === 'scheduled' && (
                <button className="primary-button" onClick={() => action('take')} disabled={saving}>Взять в работу</button>
              )}
              {item.status === 'in_progress' && (
                <button className="primary-button" onClick={() => action('check')} disabled={saving}>Проверено</button>
              )}
              <button className="secondary-button" onClick={() => setCommentOpen(value => !value)} disabled={saving}>Комментарий</button>
            </div>
            {commentOpen && (
              <div className="comment-box">
                <textarea value={comment} onChange={event => setComment(event.target.value)} placeholder="Комментарий по ППР" />
                <button className="primary-button" onClick={saveComment} disabled={saving || !comment.trim()}>Сохранить</button>
              </div>
            )}
          </section>
        )}

        <section className="section">
          <h2>История действий</h2>
          {item.audit_log.length > 0 ? (
            <ul className="history">
              {item.audit_log.map((entry, index) => (
                <li key={index}>
                  <b>{actionMap[entry.action] || entry.action}</b>
                  <span>{entry.user_name || 'system'}</span>
                  <time>{formatDateTime(entry.created_at)}</time>
                  {entry.comment && <p>{entry.comment}</p>}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">Истории действий пока нет.</p>
          )}
        </section>
      </article>
    </main>
  )
}

export default function App() {
  const [currentUser, setCurrentUser] = useState<AppUser | null>(null)
  const [tab, setTab] = useState<TabKey>('today')
  const [items, setItems] = useState<PprCard[]>([])
  const [selected, setSelected] = useState<PprCard | null>(null)
  const [editing, setEditing] = useState<{ mode: 'create' | 'edit'; item?: PprCard } | null>(null)
  const [includeArchived, setIncludeArchived] = useState(false)
  const [filters, setFilters] = useState<FiltersState>(() => emptyFilters())
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [sort, setSort] = useState<PprSort>('date_asc')
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [dashboard, setDashboard] = useState<DashboardSummary | null>(null)
  const [authLoading, setAuthLoading] = useState(true)
  const [accessDenied, setAccessDenied] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [deepLinkError, setDeepLinkError] = useState('')
  const [counts, setCounts] = useState<Partial<Record<TabKey, number>>>({})
  const deepLinkHandledRef = useRef(false)

  const notificationFromUrl = useMemo(() => getStartNotificationId(), [])

  function buildListQuery(nextPage = page): PprListQuery {
    return {
      search: filters.search,
      status: filters.status,
      project: filters.project,
      date_from: filters.date_from,
      date_to: filters.date_to,
      date_state: filters.date_state,
      checker: filters.checker,
      notify: boolFilter(filters.notify),
      outlook: boolFilter(filters.outlook),
      include_archived: includeArchived || filters.status === 'archived',
      sort,
      page: nextPage,
      page_size: pageSize,
      quick_filter: filters.quick_filter
    }
  }

  async function loadCounts() {
    if (!currentUser) return
    try {
      const [summary, all, unverified, missingDate] = await Promise.all([api.dashboardSummary(), api.all(includeArchived), api.unverified(), api.missingDate()])
      setDashboard(summary)
      setCounts({
        all: all.length,
        unverified: unverified.length,
        missingDate: missingDate.length
      })
    } catch {
      setCounts({})
    }
  }

  async function load(nextPage = 1, append = false) {
    if (!currentUser) return
    setLoading(true)
    setError('')
    try {
      const data = await api.pprList(buildListQuery(nextPage))
      setItems(current => append ? [...current, ...data.items] : data.items)
      setTotal(data.total)
      setPage(data.page)
      setCounts(value => ({ ...value, [tab]: data.total }))
    } catch (e: any) {
      setError(e.message)
      setItems([])
      setTotal(0)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    async function loadMe() {
      setAuthLoading(true)
      setAccessDenied('')
      const authMode = getAuthMode()
      if (authMode === 'none') {
        setCurrentUser(null)
        setAccessDenied('Откройте Mini App из Telegram.')
        setAuthLoading(false)
        return
      }
      try {
        const me = await api.me()
        setCurrentUser(me)
      } catch (e: any) {
        setCurrentUser(null)
        if (e instanceof ApiError && e.status === 403) {
          setAccessDenied('Нет доступа. Обратитесь к администратору.')
        } else if (e instanceof ApiError && e.status === 401 && authMode === 'telegram') {
          setAccessDenied('Не удалось подтвердить Telegram-сессию. Откройте Mini App из Telegram заново.')
        } else if (e instanceof ApiError && e.status === 401 && authMode === 'dev') {
          setAccessDenied('Dev-доступ не включен на backend или указан неверный dev-пользователь.')
        } else {
          setAccessDenied(e.message || 'Нет доступа. Обратитесь к администратору.')
        }
      } finally {
        setAuthLoading(false)
      }
    }

    loadMe()
  }, [])

  useEffect(() => {
    if (!currentUser || deepLinkHandledRef.current) return

    deepLinkHandledRef.current = true
    if (!notificationFromUrl) return

    async function openInitialNotificationCard() {
      setDeepLinkError('')
      try {
        const card = await api.card(Number(notificationFromUrl))
        setSelected(card)
      } catch (e: any) {
        setDeepLinkError(e.message || `Не удалось открыть карточку #${notificationFromUrl}.`)
      }
    }

    openInitialNotificationCard()
  }, [currentUser, notificationFromUrl])

  useEffect(() => {
    if (currentUser) {
      loadCounts()
    }
  }, [currentUser])

  useEffect(() => {
    if (currentUser && currentUser.role !== 'admin' && ['users', 'importExcel', 'deliveryErrors'].includes(tab)) {
      setTab('today')
    }
  }, [currentUser, tab])

  useEffect(() => {
    if (currentUser && tab !== 'users' && tab !== 'importExcel' && tab !== 'deliveryErrors') {
      load(1, false)
    }
  }, [tab, currentUser, includeArchived, filters, sort])

  function switchTab(nextTab: TabKey) {
    setTab(nextTab)
    setPage(1)
    setFilters(current => {
      if (nextTab === 'today') return { ...current, quick_filter: 'today', status: '', date_state: '' }
      if (nextTab === 'unverified') return { ...current, quick_filter: 'unverified', status: '', date_state: '' }
      if (nextTab === 'missingDate') return { ...current, quick_filter: 'missing_date', status: '', date_state: 'missing' }
      if (nextTab === 'all') return { ...current, quick_filter: '', date_state: '' }
      return current
    })
  }

  async function openCard(item: PprCard) {
    setDeepLinkError('')
    const card = await api.ppr(item.event.id)
    setSelected(card)
  }

  async function saveChangedCard(item: PprCard) {
    setSelected(item)
    setEditing(null)
    await loadCounts()
    await load(1, false)
  }

  async function archiveCard(item: PprCard) {
    setError('')
    try {
      const force = item.status === 'in_progress'
        ? window.confirm('ППР находится в работе. Принудительно архивировать?')
        : false
      if (item.status === 'in_progress' && !force) return
      const updated = await api.archivePpr(item.event.id, force)
      setSelected(updated)
      await loadCounts()
      await load(1, false)
    } catch (e: any) {
      setError(e.message)
    }
  }

  async function restoreCard(item: PprCard) {
    setError('')
    try {
      const updated = await api.restorePpr(item.event.id)
      setSelected(updated)
      await loadCounts()
      await load(1, false)
    } catch (e: any) {
      setError(e.message)
    }
  }

  function resetFiltersForCurrentTab() {
    const base = emptyFilters()
    if (tab === 'all') base.quick_filter = ''
    if (tab === 'unverified') base.quick_filter = 'unverified'
    if (tab === 'missingDate') {
      base.quick_filter = 'missing_date'
      base.date_state = 'missing'
    }
    setFilters(base)
    setSort('date_asc')
    setPage(1)
  }

  function applyDashboardFilter(item: typeof dashboardLabels[number]) {
    setTab('all')
    setPage(1)
    setFilters(current => ({
      ...current,
      quick_filter: item.quick || '',
      status: item.status || '',
      date_state: item.key === 'missing_date' ? 'missing' : '',
    }))
    if (item.key === 'archived') setIncludeArchived(true)
  }

  function setFilter<K extends keyof FiltersState>(key: K, value: FiltersState[K]) {
    setPage(1)
    setFilters(current => ({ ...current, [key]: value }))
  }

  const activeFilterLabels = [
    filters.search && `поиск: ${filters.search}`,
    filters.status && `статус: ${statusMap[filters.status] || filters.status}`,
    filters.project && `проект: ${filters.project}`,
    filters.date_from && `от ${filters.date_from}`,
    filters.date_to && `до ${filters.date_to}`,
    filters.date_state === 'present' && 'есть дата',
    filters.date_state === 'missing' && 'без даты',
    filters.checker && `checker: ${filters.checker}`,
    filters.notify === 'true' && 'уведомление включено',
    filters.notify === 'false' && 'уведомление выключено',
    filters.outlook === 'true' && 'есть Outlook',
    filters.outlook === 'false' && 'нет Outlook',
    includeArchived && 'архив включен',
    filters.quick_filter && `быстрый: ${filters.quick_filter}`,
  ].filter(Boolean) as string[]

  if (authLoading) {
    return (
      <main className="app-shell">
        <div className="loading">Загрузка...</div>
      </main>
    )
  }

  if (!currentUser) {
    return (
      <main className="app-shell">
        <article className="card detail-card">
          <h1>{accessDenied.startsWith('Откройте') ? 'Требуется Telegram' : 'Нет доступа'}</h1>
          <Notice tone="error">{accessDenied || 'Нет доступа. Обратитесь к администратору.'}</Notice>
        </article>
      </main>
    )
  }

  if (editing) {
    return (
      <PprFormView
        mode={editing.mode}
        initial={editing.item}
        onCancel={() => setEditing(null)}
        onSaved={saveChangedCard}
      />
    )
  }

  if (selected) {
    return (
      <CardView
        item={selected}
        currentUser={currentUser}
        onEdit={item => setEditing({ mode: 'edit', item })}
        onArchive={archiveCard}
        onRestore={restoreCard}
        onBack={() => setSelected(null)}
        onChanged={setSelected}
      />
    )
  }

  const visibleTabs = currentUser.role === 'admin' ? adminTabs : tabs

  return (
    <main className="app-shell">
      <header className="screen-header">
        <div>
          <span className="eyebrow">Mini App</span>
          <h1>ППР</h1>
          <span className="user-role">{currentUser.role}</span>
        </div>
        <div className="screen-count">{loading ? '...' : tab === 'users' ? counts.users ?? 0 : tab === 'deliveryErrors' ? counts.deliveryErrors ?? 0 : tab === 'importExcel' ? '-' : total}</div>
      </header>

      {dashboard && (
        <section className="dashboard-grid">
          {dashboardLabels
            .filter(item => currentUser.role === 'admin' || !item.adminOnly)
            .map(item => (
              <button key={item.key} className="dashboard-tile" onClick={() => applyDashboardFilter(item)}>
                <span>{item.label}</span>
                <b>{dashboard[item.key]}</b>
              </button>
            ))}
        </section>
      )}

      {currentUser.role === 'admin' && tab !== 'users' && tab !== 'importExcel' && tab !== 'deliveryErrors' && (
        <div className="admin-toolbar">
          <button className="secondary-button" onClick={() => setEditing({ mode: 'create' })}>Добавить ППР</button>
          {tab === 'all' && (
            <button className="secondary-button" onClick={() => setIncludeArchived(value => !value)}>
              {includeArchived ? 'Скрыть архив' : 'Показать архив'}
            </button>
          )}
        </div>
      )}

      <nav className="tabs" aria-label="Навигация по ППР">
        {visibleTabs.map(item => (
          <button key={item.key} className={tab === item.key ? 'active' : ''} onClick={() => switchTab(item.key)}>
            <span>{item.label}</span>
            {counts[item.key] !== undefined && <b>{counts[item.key]}</b>}
          </button>
        ))}
      </nav>

      {tab === 'users' ? (
        <UserManagementView onCountChange={count => setCounts(value => ({ ...value, users: count }))} />
      ) : tab === 'deliveryErrors' ? (
        <DeliveryErrorsView onCountChange={count => setCounts(value => ({ ...value, deliveryErrors: count }))} />
      ) : tab === 'importExcel' ? (
        <ImportExcelView />
      ) : (
        <>
          {tab === 'missingDate' && (
            <Notice>Эти ППР импортированы, но по ним не будет уведомлений, пока не заполнена дата.</Notice>
          )}

          {deepLinkError && <Notice tone="error">{deepLinkError}</Notice>}
          {error && <Notice tone="error">{error}</Notice>}

          <section className="list-controls">
            <div className="search-row">
              <input value={filters.search} onChange={event => setFilter('search', event.target.value)} placeholder="Поиск по ППР, ответственным, комментариям" />
              <button className="secondary-button" onClick={() => setFiltersOpen(value => !value)}>Фильтры</button>
            </div>

            <div className="quick-filters">
              <button onClick={() => setFilter('quick_filter', 'mine_in_progress')}>Мои в работе</button>
              <button onClick={() => setFilter('quick_filter', 'overdue')}>Просроченные</button>
              <button onClick={() => setFilter('quick_filter', 'today')}>Сегодня</button>
              <button onClick={() => { setFilter('quick_filter', 'missing_date'); setFilter('date_state', 'missing') }}>Без даты</button>
              <button onClick={() => setFilter('quick_filter', 'unverified')}>Не проверены</button>
            </div>

            <div className="sort-row">
              <label>
                <span>Сортировка</span>
                <select value={sort} onChange={event => { setPage(1); setSort(event.target.value as PprSort) }}>
                  <option value="date_asc">Дата по возрастанию</option>
                  <option value="date_desc">Дата по убыванию</option>
                  <option value="overdue_first">Сначала просроченные</option>
                  <option value="updated_desc">Недавно измененные</option>
                  <option value="title">По названию</option>
                  <option value="project">По проекту</option>
                </select>
              </label>
              <button className="secondary-button" onClick={resetFiltersForCurrentTab}>Сбросить</button>
            </div>

            {filtersOpen && (
              <div className="filters-panel">
                <label>
                  <span>Статус</span>
                  <select value={filters.status} onChange={event => setFilter('status', event.target.value)}>
                    <option value="">Любой</option>
                    <option value="scheduled">scheduled</option>
                    <option value="in_progress">in_progress</option>
                    <option value="verified">verified</option>
                    {currentUser.role === 'admin' && <option value="archived">archived</option>}
                    <option value="cancelled">cancelled</option>
                  </select>
                </label>
                <label><span>Проект</span><input value={filters.project} onChange={event => setFilter('project', event.target.value)} /></label>
                <label><span>Дата от</span><input type="date" value={filters.date_from} onChange={event => setFilter('date_from', event.target.value)} /></label>
                <label><span>Дата до</span><input type="date" value={filters.date_to} onChange={event => setFilter('date_to', event.target.value)} /></label>
                <label>
                  <span>Дата</span>
                  <select value={filters.date_state} onChange={event => setFilter('date_state', event.target.value as FiltersState['date_state'])}>
                    <option value="">Любая</option>
                    <option value="present">Есть дата</option>
                    <option value="missing">Без даты</option>
                  </select>
                </label>
                <label><span>Checker</span><input value={filters.checker} onChange={event => setFilter('checker', event.target.value)} /></label>
                <label>
                  <span>Уведомление</span>
                  <select value={filters.notify} onChange={event => setFilter('notify', event.target.value as FiltersState['notify'])}>
                    <option value="">Любое</option>
                    <option value="true">Включено</option>
                    <option value="false">Выключено</option>
                  </select>
                </label>
                <label>
                  <span>Outlook</span>
                  <select value={filters.outlook} onChange={event => setFilter('outlook', event.target.value as FiltersState['outlook'])}>
                    <option value="">Любой</option>
                    <option value="true">Есть ссылка</option>
                    <option value="false">Нет ссылки</option>
                  </select>
                </label>
                {currentUser.role === 'admin' && (
                  <label className="checkbox-row">
                    <input type="checkbox" checked={includeArchived} onChange={event => setIncludeArchived(event.target.checked)} />
                    <span>Показывать архивные</span>
                  </label>
                )}
              </div>
            )}

            {activeFilterLabels.length > 0 && (
              <div className="active-filters">
                {activeFilterLabels.map(item => <span key={item}>{item}</span>)}
              </div>
            )}
          </section>

          {loading && <div className="loading">Загрузка...</div>}
          {!loading && !error && items.length === 0 && <div className="empty-state">По заданным условиям ППР не найдены.</div>}

          <div className="list">
            {items.map(item => <ListItem key={`${item.notification_id ?? 'event'}-${item.event.id}`} item={item} onOpen={openCard} />)}
          </div>
          {items.length < total && (
            <button className="load-more-button" onClick={() => load(page + 1, true)} disabled={loading}>
              Показать еще
            </button>
          )}
        </>
      )}
    </main>
  )
}
