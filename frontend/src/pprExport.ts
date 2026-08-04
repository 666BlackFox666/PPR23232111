import type { PprExportFile } from './api'
import type { AppUser } from './types'


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

export async function runPprExportDownload(
  download: () => Promise<PprExportFile>,
  save: (file: PprExportFile) => void,
  setLoading: (loading: boolean) => void,
  setError: (error: string) => void
) {
  setLoading(true)
  setError('')
  try {
    save(await download())
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
