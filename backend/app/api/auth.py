import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import AppUser
from app.db.session import get_db
from app.services.user_service import get_or_sync_user


def validate_telegram_init_data(init_data: str) -> dict:
    """Проверка initData Telegram Mini App.

    Возвращает dict с данными, если подпись валидна.
    """
    settings = get_settings()
    if not init_data:
        raise ValueError("Telegram initData is required")
    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required to validate Telegram initData")

    data = dict(parse_qsl(init_data, strict_parsing=True))
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise ValueError("Telegram initData hash is missing")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", settings.telegram_bot_token.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise ValueError("Invalid Telegram initData hash")

    auth_date_raw = data.get("auth_date")
    if not auth_date_raw:
        raise ValueError("Telegram initData auth_date is missing")
    try:
        auth_date = int(auth_date_raw)
    except ValueError as exc:
        raise ValueError("Telegram initData auth_date is invalid") from exc

    max_age = settings.telegram_webapp_auth_max_age_seconds
    if max_age > 0 and int(time.time()) - auth_date > max_age:
        raise ValueError("Telegram initData is expired")
    return data


def parse_telegram_user(data: dict) -> tuple[str, str | None, str | None]:
    user = data.get("user")
    if isinstance(user, str):
        user = json.loads(user)
    if not isinstance(user, dict):
        raise ValueError("Telegram user data is missing")

    telegram_id = str(user.get("id") or "")
    if not telegram_id:
        raise ValueError("Telegram user id is missing")
    username = user.get("username")
    full_name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])).strip() or None
    return telegram_id, username, full_name


def dev_telegram_user(
    x_dev_telegram_id: str,
    x_dev_username: str,
    x_dev_full_name: str,
) -> tuple[str, str | None, str | None]:
    if not x_dev_telegram_id:
        raise HTTPException(status_code=401, detail="X-Dev-Telegram-Id is required when Telegram initData is absent")
    return x_dev_telegram_id, x_dev_username or None, x_dev_full_name or None


def get_current_user(
    x_telegram_init_data: str = Header(default=""),
    x_dev_telegram_id: str = Header(default=""),
    x_dev_username: str = Header(default=""),
    x_dev_full_name: str = Header(default=""),
    db: Session = Depends(get_db),
) -> AppUser:
    settings = get_settings()
    try:
        if x_telegram_init_data:
            telegram_id, username, full_name = parse_telegram_user(validate_telegram_init_data(x_telegram_init_data))
        elif settings.dev_commands_enabled:
            telegram_id, username, full_name = dev_telegram_user(x_dev_telegram_id, x_dev_username, x_dev_full_name)
        else:
            raise HTTPException(status_code=401, detail="Telegram initData is required")
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    user = get_or_sync_user(db, telegram_id, username, full_name)
    if user is None:
        raise HTTPException(status_code=403, detail="User is not allowed")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User is inactive")
    return user


def require_roles(*roles: str):
    def dependency(user: AppUser = Depends(get_current_user)) -> AppUser:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return dependency
