import type { TelegramWebApp, TelegramWebAppEventHandler } from './telegramWebApp'

export const FULLSCREEN_MIN_WIDTH = 768
export const FULLSCREEN_ERROR_MESSAGE = 'Не удалось изменить полноэкранный режим.'

export function supportsTelegramFullscreen(webApp: TelegramWebApp | undefined): webApp is TelegramWebApp & Required<Pick<TelegramWebApp, 'requestFullscreen' | 'exitFullscreen'>> {
  return Boolean(
    webApp &&
    typeof webApp.requestFullscreen === 'function' &&
    typeof webApp.exitFullscreen === 'function'
  )
}

export function canShowFullscreenControl(webApp: TelegramWebApp | undefined, viewportWidth: number): boolean {
  return viewportWidth >= FULLSCREEN_MIN_WIDTH && supportsTelegramFullscreen(webApp)
}

export function fullscreenButtonLabel(isFullscreen: boolean): string {
  return isFullscreen ? 'Выйти из полноэкранного режима' : 'На весь экран'
}

export async function toggleTelegramFullscreen(webApp: TelegramWebApp | undefined): Promise<boolean> {
  if (!supportsTelegramFullscreen(webApp)) {
    throw new Error('Telegram fullscreen API is unavailable')
  }
  if (webApp.isFullscreen) {
    await webApp.exitFullscreen()
    return false
  }
  await webApp.requestFullscreen()
  return true
}

export function subscribeToFullscreenChanges(
  webApp: TelegramWebApp | undefined,
  handler: TelegramWebAppEventHandler
): () => void {
  if (!webApp?.onEvent || !webApp.offEvent) return () => {}
  webApp.onEvent('fullscreenChanged', handler)
  return () => webApp.offEvent?.('fullscreenChanged', handler)
}
