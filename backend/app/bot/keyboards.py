from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from app.config import get_settings
from app.services.statuses import PPR_STATUS_IN_PROGRESS, PPR_STATUS_SCHEDULED


def is_public_card_url(webapp_url: str | None = None) -> bool:
    if webapp_url is None:
        return get_settings().public_webapp_enabled
    value = webapp_url
    normalized = value.strip().lower()
    if not normalized:
        return False
    if normalized.startswith(("http://127.0.0.1", "https://127.0.0.1", "http://localhost", "https://localhost", "localhost")):
        return False
    return normalized.startswith("https://")


def build_card_deep_link(notification_id: int) -> str | None:
    settings = get_settings()
    if not settings.public_webapp_enabled:
        return None
    bot_username = settings.telegram_bot_username.strip().lstrip("@")
    if not bot_username:
        return None

    start_param = quote(f"notification_{notification_id}", safe="")
    short_name = settings.telegram_miniapp_short_name.strip().strip("/")
    if short_name:
        return f"https://t.me/{bot_username}/{quote(short_name, safe='')}?startapp={start_param}"
    return f"https://t.me/{bot_username}?startapp={start_param}"


def is_card_deep_link_configured() -> bool:
    settings = get_settings()
    return settings.public_webapp_enabled and bool(settings.telegram_bot_username.strip().lstrip("@"))


def notification_keyboard(notification_id: int, ppr_status: str) -> InlineKeyboardMarkup | None:
    buttons = []
    row = []
    if ppr_status == PPR_STATUS_SCHEDULED:
        row.append(InlineKeyboardButton(text="👀 Взять в работу", callback_data=f"take:{notification_id}"))
    if ppr_status == PPR_STATUS_IN_PROGRESS:
        row.append(InlineKeyboardButton(text="✅ Проверено", callback_data=f"check:{notification_id}"))
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton(text="ℹ️ Подробнее", callback_data=f"details:{notification_id}")])

    card_url = build_card_deep_link(notification_id)
    if card_url:
        buttons.append([InlineKeyboardButton(text="📋 Открыть карточку", url=card_url)])

    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=buttons)
