"""Twilio REST client for reconciliation — read-only Calls pull (ARCHITECTURE.md #6)."""

from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import settings
from app.providers.base import NormalizedCallEvent
from app.providers.cxml import normalize_call

API_ROOT = "https://api.twilio.com/2010-04-01"


async def fetch_recent_calls(window_hours: int) -> list[NormalizedCallEvent]:
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        return []
    since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).strftime("%Y-%m-%d")
    url = f"{API_ROOT}/Accounts/{settings.TWILIO_ACCOUNT_SID}/Calls.json"
    auth = (settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    events: list[NormalizedCallEvent] = []
    async with httpx.AsyncClient(timeout=30) as client:
        params = {"StartTime>": since, "PageSize": "1000"}
        next_url: str | None = url
        while next_url:
            resp = await client.get(next_url, params=params if next_url == url else None, auth=auth)
            resp.raise_for_status()
            data = resp.json()
            events.extend(normalize_call(c) for c in data.get("calls", []))
            nxt = data.get("next_page_uri")
            next_url = f"https://api.twilio.com{nxt}" if nxt else None
    return events


async def delete_recording(recording_sid: str) -> None:
    """Delete the provider-side copy so Twilio never bills us for storage.
    Tolerant of 404 (already gone) — idempotent under retries."""
    if not (settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN and recording_sid):
        return
    url = f"{API_ROOT}/Accounts/{settings.TWILIO_ACCOUNT_SID}/Recordings/{recording_sid}.json"
    auth = (settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.delete(url, auth=auth)
        if resp.status_code not in (200, 204, 404):
            resp.raise_for_status()
