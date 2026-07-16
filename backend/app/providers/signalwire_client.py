"""SignalWire Compatibility (LaML) REST client for reconciliation. Same Calls resource
shape as Twilio, served from the project's space URL.

Feeds the app entirely by polling — this is what makes the Call Flow Builder (which does
not POST granular status callbacks to us) work: we read Calls + Recordings from the API
instead of relying on webhooks. Recordings are downloaded then deleted remotely to avoid
provider-side storage charges (see handlers.handle_recording_fetch)."""

from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import settings
from app.providers.base import NormalizedCallEvent, NormalizedRecordingEvent
from app.providers.cxml import normalize_call, to_int


def _configured() -> bool:
    return bool(
        settings.SIGNALWIRE_PROJECT_ID
        and settings.SIGNALWIRE_AUTH_TOKEN
        and settings.SIGNALWIRE_SPACE_URL
    )


def _base() -> str:
    return f"https://{settings.SIGNALWIRE_SPACE_URL}/api/laml/2010-04-01"


def _auth() -> tuple[str, str]:
    return (settings.SIGNALWIRE_PROJECT_ID, settings.SIGNALWIRE_AUTH_TOKEN)


async def fetch_recent_calls(window_hours: int) -> list[NormalizedCallEvent]:
    if not _configured():
        return []
    since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).strftime("%Y-%m-%d")
    account = f"{_base()}/Accounts/{settings.SIGNALWIRE_PROJECT_ID}"
    url = f"{account}/Calls.json"
    events: list[NormalizedCallEvent] = []
    async with httpx.AsyncClient(timeout=30) as client:
        params = {"StartTime>": since, "PageSize": "1000"}
        next_url: str | None = url
        while next_url:
            resp = await client.get(next_url, params=params if next_url == url else None, auth=_auth())
            resp.raise_for_status()
            data = resp.json()
            events.extend(normalize_call(c) for c in data.get("calls", []))
            nxt = data.get("next_page_uri")
            next_url = f"https://{settings.SIGNALWIRE_SPACE_URL}{nxt}" if nxt else None
    return events


async def fetch_recent_recordings(window_hours: int) -> list[NormalizedRecordingEvent]:
    """Pull recordings created in the window. Used because the Call Flow Builder's
    Start Call Recording node stores to SignalWire but does not reliably POST a
    recordingStatusCallback to us — so we discover recordings by polling instead."""
    if not _configured():
        return []
    since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).strftime("%Y-%m-%d")
    account = f"{_base()}/Accounts/{settings.SIGNALWIRE_PROJECT_ID}"
    url = f"{account}/Recordings.json"
    recordings: list[NormalizedRecordingEvent] = []
    async with httpx.AsyncClient(timeout=30) as client:
        params = {"DateCreated>": since, "PageSize": "1000"}
        next_url: str | None = url
        while next_url:
            resp = await client.get(next_url, params=params if next_url == url else None, auth=_auth())
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
            next_url = f"https://{settings.SIGNALWIRE_SPACE_URL}{nxt}" if nxt else None
    return recordings


async def delete_recording(recording_sid: str) -> None:
    """Delete the provider-side copy so SignalWire never bills us for storage.
    Tolerant of 404 (already gone) — idempotent under retries."""
    if not (_configured() and recording_sid):
        return
    url = f"{_base()}/Accounts/{settings.SIGNALWIRE_PROJECT_ID}/Recordings/{recording_sid}.json"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.delete(url, auth=_auth())
        if resp.status_code not in (200, 204, 404):
            resp.raise_for_status()
