from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


DOWNLOAD_TOKEN_TTL_SECONDS = 120


@dataclass(frozen=True)
class PprExportDownloadGrant:
    user_id: int
    generated_at: datetime
    expires_at: datetime


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def create_ppr_export_download_token(
    user_id: int,
    secret: str,
    now: datetime | None = None,
) -> tuple[str, PprExportDownloadGrant]:
    if not secret:
        raise ValueError("Telegram bot token is required for export download links")
    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expires_at = generated_at + timedelta(seconds=DOWNLOAD_TOKEN_TTL_SECONDS)
    payload = json.dumps(
        {
            "user_id": user_id,
            "generated_at": int(generated_at.timestamp()),
            "expires_at": int(expires_at.timestamp()),
            "nonce": secrets.token_urlsafe(12),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    grant = PprExportDownloadGrant(user_id=user_id, generated_at=generated_at, expires_at=expires_at)
    return f"{_encode(payload)}.{_encode(signature)}", grant


def read_ppr_export_download_token(
    token: str,
    secret: str,
    now: datetime | None = None,
) -> PprExportDownloadGrant | None:
    if not secret:
        return None
    try:
        payload_text, signature_text = token.split(".", 1)
        payload = _decode(payload_text)
        signature = _decode(signature_text)
        expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        values = json.loads(payload)
        generated_at = datetime.fromtimestamp(int(values["generated_at"]), timezone.utc)
        expires_at = datetime.fromtimestamp(int(values["expires_at"]), timezone.utc)
        user_id = int(values["user_id"])
    except (TypeError, ValueError, KeyError, binascii.Error, json.JSONDecodeError):
        return None
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if expires_at <= current or generated_at > current + timedelta(seconds=30):
        return None
    return PprExportDownloadGrant(user_id=user_id, generated_at=generated_at, expires_at=expires_at)
