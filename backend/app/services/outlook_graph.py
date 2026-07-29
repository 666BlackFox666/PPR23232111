from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from difflib import SequenceMatcher
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import PprEvent, PprNotification

logger = logging.getLogger(__name__)


class OutlookGraphDisabledError(RuntimeError):
    pass


class OutlookGraphConfigError(RuntimeError):
    pass


class OutlookGraphRequestError(RuntimeError):
    pass


class OutlookGraphNoDateError(RuntimeError):
    pass


class OutlookGraphClient:
    """Minimal Microsoft Graph client for finding Outlook calendar events."""

    graph_base_url = "https://graph.microsoft.com/v1.0"
    match_threshold = 0.65

    def __init__(self):
        self.settings = get_settings()
        self._token: str | None = None

    def _ensure_enabled(self) -> None:
        if not self.settings.outlook_enabled:
            raise OutlookGraphDisabledError("Outlook integration is disabled.")

    def _ensure_configured(self) -> None:
        missing = [
            name
            for name, value in {
                "OUTLOOK_TENANT_ID": self.settings.outlook_tenant_id,
                "OUTLOOK_CLIENT_ID": self.settings.outlook_client_id,
                "OUTLOOK_CLIENT_SECRET": self.settings.outlook_client_secret,
                "OUTLOOK_USER_ID": self.settings.outlook_user_id,
            }.items()
            if not value
        ]
        if missing:
            raise OutlookGraphConfigError(f"Missing Outlook settings: {', '.join(missing)}.")

    def _timezone(self):
        try:
            return ZoneInfo(self.settings.default_timezone)
        except ZoneInfoNotFoundError:
            logger.warning("Unknown timezone %s, falling back to UTC for Outlook Graph.", self.settings.default_timezone)
            return ZoneInfo("UTC")

    async def _request_json(self, method: str, url: str, **kwargs) -> dict:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(method, url, **kwargs) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        raise OutlookGraphRequestError(f"Graph request failed: HTTP {resp.status}: {body[:500]}")
                    return await resp.json()
        except aiohttp.ClientError as exc:
            raise OutlookGraphRequestError(f"Graph request failed: {exc}") from exc

    async def _get_token(self) -> str:
        self._ensure_enabled()
        self._ensure_configured()
        if self._token:
            return self._token

        url = f"https://login.microsoftonline.com/{self.settings.outlook_tenant_id}/oauth2/v2.0/token"
        payload = await self._request_json(
            "POST",
            url,
            data={
                "client_id": self.settings.outlook_client_id,
                "client_secret": self.settings.outlook_client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
        )
        self._token = payload["access_token"]
        return self._token

    @staticmethod
    def _normalize_title(value: str) -> str:
        normalized = value.lower().strip()
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized

    @classmethod
    def _score(cls, expected: str, actual: str) -> float:
        expected_norm = cls._normalize_title(expected)
        actual_norm = cls._normalize_title(actual)
        if not expected_norm or not actual_norm:
            return 0.0
        if expected_norm == actual_norm:
            return 1.0
        if expected_norm in actual_norm or actual_norm in expected_norm:
            return 0.92
        return SequenceMatcher(None, expected_norm, actual_norm).ratio()

    def _date_range(self, event_date: date) -> tuple[str, str]:
        tz = self._timezone()
        window_days = max(1, self.settings.outlook_search_days_window)
        start_dt = datetime.combine(event_date, time.min, tzinfo=tz)
        end_dt = datetime.combine(event_date + timedelta(days=window_days), time.min, tzinfo=tz)
        return start_dt.isoformat(), end_dt.isoformat()

    @staticmethod
    def _parse_graph_date(value: str | None) -> date | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None

    def _event_matches_date(self, item: dict, event_date: date) -> bool:
        start = item.get("start") or {}
        end = item.get("end") or {}
        start_date = self._parse_graph_date(start.get("dateTime"))
        end_date = self._parse_graph_date(end.get("dateTime"))
        return start_date == event_date or end_date == event_date

    async def find_calendar_event(self, title: str, event_date: date) -> dict | None:
        token = await self._get_token()
        start_dt, end_dt = self._date_range(event_date)
        user_id = quote(self.settings.outlook_user_id, safe="")
        url = (
            f"{self.graph_base_url}/users/{user_id}/calendarView"
            f"?startDateTime={quote(start_dt)}&endDateTime={quote(end_dt)}"
            "&$select=subject,start,end,webLink"
            "&$orderby=start/dateTime"
            "&$top=50"
        )
        payload = await self._request_json(
            "GET",
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Prefer": f'outlook.timezone="{self.settings.default_timezone}"',
            },
        )

        best_item = None
        best_score = 0.0
        for item in payload.get("value", []):
            if not self._event_matches_date(item, event_date):
                continue
            score = self._score(title, item.get("subject", ""))
            if score > best_score:
                best_score = score
                best_item = item

        if best_item and best_score >= self.match_threshold:
            return best_item
        return None

    async def find_calendar_link(self, title: str, event_date: date) -> str | None:
        item = await self.find_calendar_event(title, event_date)
        return item.get("webLink") if item else None


def outlook_integration_configured() -> bool:
    settings = get_settings()
    return bool(
        settings.outlook_enabled
        and settings.outlook_tenant_id
        and settings.outlook_client_id
        and settings.outlook_client_secret
        and settings.outlook_user_id
    )


def _rollback_outlook_sync(db: Session) -> None:
    try:
        db.rollback()
    except Exception as exc:
        logger.exception("Automatic Outlook synchronization rollback failed: %s", exc)


async def sync_imported_events_outlook_links(db: Session, events: list[PprEvent]) -> None:
    """Best-effort Outlook enrichment for events changed by one Excel import."""
    try:
        candidates = [event for event in events if event.date and not event.outlook_link]
        if not candidates:
            return
        if not outlook_integration_configured():
            logger.info("Automatic Outlook synchronization skipped: integration is not configured.")
            return

        client = OutlookGraphClient()
        for event in candidates:
            event_id = event.id
            try:
                if event.outlook_link:
                    continue
                link = await client.find_calendar_link(event.title, event.date)
                if not link:
                    logger.info("Automatic Outlook synchronization: event not found for PPR %s.", event_id)
                    continue
                event.outlook_link = link
                event.updated_at = datetime.utcnow()
                db.commit()
                logger.info("Automatic Outlook synchronization: link found for PPR %s.", event_id)
            except Exception as exc:
                _rollback_outlook_sync(db)
                logger.exception("Automatic Outlook synchronization failed for PPR %s: %s", event_id, exc)
    except Exception as exc:
        _rollback_outlook_sync(db)
        logger.exception("Automatic Outlook synchronization setup failed: %s", exc)


async def sync_notification_outlook_link(db: Session, notification: PprNotification) -> str | None:
    if not notification.event.date:
        raise OutlookGraphNoDateError("PPR event has no date.")

    link = await OutlookGraphClient().find_calendar_link(notification.event.title, notification.event.date)
    if not link:
        return None

    notification.event.outlook_link = link
    notification.event.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(notification)
    return link
