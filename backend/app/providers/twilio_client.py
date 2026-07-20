"""Twilio REST client for reconciliation — read-only Calls pull (ARCHITECTURE.md #6)."""

from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import settings
from app.providers.base import NormalizedCallEvent, NormalizedRecordingEvent
from app.providers.cxml import normalize_call, to_int

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


async def fetch_recent_recordings(window_hours: int) -> list[NormalizedRecordingEvent]:
    """Pull recordings created in the window (Twilio Recordings resource).

    Twilio's Studio "Connect Call To" widget records via its "Start Recording" toggle,
    but that widget exposes no recordingStatusCallback field — so Twilio never POSTs us a
    /recording webhook for Studio-recorded calls. We discover those recordings by polling
    instead, mirroring the SignalWire recording poll. Idempotent downstream: the reconciler
    only enqueues a fetch when we don't already hold the audio (ingest_recording_event
    upserts on the unique recording sid)."""
    if not (settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN):
        return []
    since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).strftime("%Y-%m-%d")
    account = f"{API_ROOT}/Accounts/{settings.TWILIO_ACCOUNT_SID}"
    url = f"{account}/Recordings.json"
    auth = (settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    recordings: list[NormalizedRecordingEvent] = []
    async with httpx.AsyncClient(timeout=30) as client:
        params: dict | None = {"DateCreated>": since, "PageSize": "1000"}
        next_url: str | None = url
        while next_url:
            resp = await client.get(next_url, params=params if next_url == url else None, auth=auth)
            resp.raise_for_status()
            data = resp.json()
            for r in data.get("recordings", []):
                sid = r.get("sid", "")
                recordings.append(
                    NormalizedRecordingEvent(
                        provider_call_sid=r.get("call_sid", ""),
                        provider_recording_sid=sid,
                        status=r.get("status") or "completed",
                        duration_seconds=to_int(r.get("duration")),
                        # handler appends ".mp3" to fetch the media
                        provider_url=f"{account}/Recordings/{sid}",
                        raw=r,
                    )
                )
            nxt = data.get("next_page_uri")
            next_url = f"https://api.twilio.com{nxt}" if nxt else None
    return recordings


async def fetch_incoming_phone_numbers() -> list[dict]:
    """List every phone number owned by the Twilio account.

    Twilio's IncomingPhoneNumbers resource is the same shape SignalWire copied, so
    each entry carries `sid`, `phone_number` (E.164), `friendly_name`, and `voice_url`.
    Enumerates owned resources regardless of how each number is routed. Note: for
    numbers pointed at a TwiML app/flow, `voice_url` is that flow, not a forward
    target, so it can't tell us `forwards_to`.
    """
    if not (settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN):
        return []
    url = f"{API_ROOT}/Accounts/{settings.TWILIO_ACCOUNT_SID}/IncomingPhoneNumbers.json"
    auth = (settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    numbers: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        params: dict | None = {"PageSize": "1000"}
        next_url: str | None = url
        while next_url:
            resp = await client.get(next_url, params=params if next_url == url else None, auth=auth)
            resp.raise_for_status()
            data = resp.json()
            numbers.extend(data.get("incoming_phone_numbers", []))
            nxt = data.get("next_page_uri")
            next_url = f"https://api.twilio.com{nxt}" if nxt else None
    return numbers


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
