import type { PprExportDownloadTicket, PprExportFile } from './api'
import type { AppUser } from './types'
import type { TelegramWebApp } from './telegramWebApp'


export function canShowPprExport(user: Pick<AppUser, 'role' | 'is_active'> | null) {
  return Boolean(user?.is_active && user.role === 'admin')
}

interface DownloadAnchor {
  href: string
  download: string
  style: { display: string }
  click: () => void
  remove: () => void
}

interface DownloadEnvironment {
  createObjectURL: (blob: Blob) => string
  revokeObjectURL: (url: string) => void
  createAnchor: () => DownloadAnchor
  appendAnchor: (anchor: DownloadAnchor) => void
  defer: (callback: () => void) => void
}

interface NativeDownloadEnvironment {
  currentUrl: string
  webApp: TelegramWebApp | undefined
  navigate: (url: string) => void
}

function browserDownloadEnvironment(): DownloadEnvironment {
  return {
    createObjectURL: blob => URL.createObjectURL(blob),
    revokeObjectURL: url => URL.revokeObjectURL(url),
    createAnchor: () => document.createElement('a'),
    appendAnchor: anchor => document.body.appendChild(anchor as HTMLAnchorElement),
    defer: callback => window.setTimeout(callback, 0)
  }
}

export function savePprExportFile(
  file: PprExportFile,
  environment: DownloadEnvironment = browserDownloadEnvironment()
) {
  const objectUrl = environment.createObjectURL(file.blob)
  const anchor = environment.createAnchor()
  anchor.href = objectUrl
  anchor.download = file.filename
  anchor.style.display = 'none'
  environment.appendAnchor(anchor)
  anchor.click()
  anchor.remove()
  environment.defer(() => environment.revokeObjectURL(objectUrl))
}

function nativeDownloadEnvironment(): NativeDownloadEnvironment {
  return {
    currentUrl: window.location.href,
    webApp: window.Telegram?.WebApp,
    navigate: url => window.location.assign(url)
  }
}

export function savePprExportDownload(
  ticket: PprExportDownloadTicket,
  environment: NativeDownloadEnvironment = nativeDownloadEnvironment()
): Promise<void> {
  const url = new URL(ticket.url, environment.currentUrl)
  if (url.protocol !== 'https:') {
    return Promise.reject(new Error('Ссылка скачивания должна использовать HTTPS.'))
  }
  if (environment.webApp?.downloadFile) {
    return new Promise((resolve, reject) => {
      environment.webApp?.downloadFile?.(
        { url: url.toString(), file_name: ticket.filename },
        accepted => accepted ? resolve() : reject(new Error('Скачивание отменено.'))
      )
    })
  }
  if (environment.webApp?.openLink) {
    environment.webApp.openLink(url.toString(), { try_instant_view: false })
    return Promise.resolve()
  }
  environment.navigate(url.toString())
  return Promise.resolve()
}

export async function runPprExportDownload<T>(
  download: () => Promise<T>,
  save: (file: T) => void | Promise<void>,
  setLoading: (loading: boolean) => void,
  setError: (error: string) => void
) {
  setLoading(true)
  setError('')
  try {
    await save(await download())
    return true
  } catch (error) {
    const message = error instanceof Error && error.message
      ? error.message
      : 'Не удалось скачать базу ППР.'
    setError(message)
    return false
  } finally {
    setLoading(false)
  }
}
