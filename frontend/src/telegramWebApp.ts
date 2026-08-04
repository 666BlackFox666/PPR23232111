export type TelegramWebAppEvent = 'fullscreenChanged'
export type TelegramWebAppEventHandler = () => void

export interface TelegramWebApp {
  initData?: string
  initDataUnsafe?: {
    start_param?: string
  }
  isFullscreen?: boolean
  ready?: () => void
  expand?: () => void
  requestFullscreen?: () => void | Promise<void>
  exitFullscreen?: () => void | Promise<void>
  onEvent?: (event: TelegramWebAppEvent, handler: TelegramWebAppEventHandler) => void
  offEvent?: (event: TelegramWebAppEvent, handler: TelegramWebAppEventHandler) => void
}

declare global {
  interface Window {
    Telegram?: {
      WebApp?: TelegramWebApp
    }
  }
}

export function getTelegramWebApp(): TelegramWebApp | undefined {
  return typeof window === 'undefined' ? undefined : window.Telegram?.WebApp
}
