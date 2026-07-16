"""SignalWire REST clients for reconciliation.

Two API families, kept side by side:

- The classic Compatibility (LaML) API (`fetch_recent_calls`/`fetch_recent_recordings`
  below) — mirrors Twilio's Calls/Recordings resource shape. CONFIRMED DEAD for
  Call-Flow-Builder-routed numbers: calls routed through Call Flow Builder never
  appear in this log, verified empirically against a real account (see
  SIGNALWIRE_CFB_INGESTION.md). Kept only in case some SignalWire account uses
  classic LaML-configured numbers instead.
- The modern Voice API (`fetch_recent_voice_logs`/`fetch_recordings_via_voice_logs`) —
  `GET /api/voice/logs` + `GET /api/voice/logs/{id}/events`. This is what actually
  works for Call-Flow-Builder-routed numbers: it reports the real inbound leg
  (correct `to` = the tracking number, no leg-correlation tricks needed) and, via
  each call's event timeline, the `calling_call_record` event carries the finished
  recording's URL/duration/id directly — no Call Flow Builder node configuration
  required at all.
"""

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


def _voice_base() -> str:
    return f"https://{settings.SIGNALWIRE_SPACE_URL}/api/voice"


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


_VOICE_LOG_STATUS = {"ended": "completed"}


def _normalize_voice_log(entry: dict) -> NormalizedCallEvent:
    status = _VOICE_LOG_STATUS.get(entry.get("status") or "", entry.get("status"))
    started_at = _parse_iso(entry.get("created_at"))
    duration_ms = entry.get("duration_ms")
    return NormalizedCallEvent(
        provider_call_sid=entry.get("id", ""),
        event_type=entry.get("status") or "reconciled",
        status=status,
        from_number=entry.get("from"),
        to_number=entry.get("to"),
        direction=entry.get("direction"),
        started_at=started_at,
        ended_at=(
            started_at + timedelta(milliseconds=duration_ms)
            if started_at and duration_ms else None
        ),
        duration_seconds=to_int(entry.get("duration")),
        provider_sequence=f"{entry.get('id', '')}:{entry.get('status')}",
        raw=entry,
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def fetch_recent_voice_logs(window_hours: int) -> list[dict]:
    """GET /api/voice/logs — the modern replacement for the dead Compatibility API.
    Reports every call leg (inbound and outbound) with the *correct* `to`/`from` for
    each leg — no leg-correlation tricks needed, just filter direction=="inbound"."""
    if not _configured():
        return []
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    url = f"{_voice_base()}/logs"
    logs: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        next_url: str | None = url
        params: dict | None = {"page_size": 100}
        while next_url:
            resp = await client.get(next_url, params=params, auth=_auth())
            resp.raise_for_status()
            data = resp.json()
            page = data.get("data", [])
            stop = False
            for entry in page:
                created = _parse_iso(entry.get("created_at"))
                if created and created < since:
                    stop = True
                    break
                logs.append(entry)
            if stop:
                break
            next_url = (data.get("links") or {}).get("next")
            params = None  # the `next` link already carries pagination params
    return logs


async def fetch_recent_calls_voice_logs(window_hours: int) -> list[NormalizedCallEvent]:
    return [_normalize_voice_log(e) for e in await fetch_recent_voice_logs(window_hours)]


async def fetch_recordings_via_voice_logs(window_hours: int) -> list[NormalizedRecordingEvent]:
    """There IS a standalone modern recordings resource (`/api/relay/rest/recordings`,
    used by delete_recording() below) but it doesn't expose which tracking number a
    recording belongs to — only `relay_pstn_leg_id`. Reading recording completion off
    each call's own event timeline (which we already have `to`/`from` for) avoids a
    second correlation step."""
    logs = await fetch_recent_voice_logs(window_hours)
    recordings: list[NormalizedRecordingEvent] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for entry in logs:
            if (entry.get("direction") or "").lower() != "inbound":
                continue
            log_id = entry.get("id")
            resp = await client.get(
                f"{_voice_base()}/logs/{log_id}/events",
                params={"page_size": 100},
                auth=_auth(),
            )
            if resp.status_code != 200:
                continue
            for e in resp.json().get("data", []):
                p = (e.get("details") or {}).get("params") or {}
                if e.get("name") != "calling_call_record" or p.get("state") != "finished":
                    continue
                recordings.append(
                    NormalizedRecordingEvent(
                        provider_call_sid=log_id,
                        provider_recording_sid=p.get("recording_id", ""),
                        status="completed",
                        duration_seconds=to_int(p.get("duration")),
                        provider_url=p.get("url"),
                        raw=e,
                    )
                )
    return recordings


async def delete_recording(recording_sid: str) -> None:
    """Delete the provider-side copy so SignalWire never bills us for storage.
    Tolerant of 404 (already gone) — idempotent under retries.

    Uses the modern Relay REST recordings resource (`/api/relay/rest/recordings/{id}`),
    confirmed via SignalWire's docs and a live test (204, file access revoked
    afterward). The classic Compatibility API delete endpoint used here previously
    silently no-op'd (404, swallowed as "already gone") for every Call-Flow-Builder
    recording, since those never exist in that API at all — recordings were
    downloaded but never actually deleted remotely. See SIGNALWIRE_CFB_INGESTION.md.
    """
    if not (_configured() and recording_sid):
        return
    url = f"https://{settings.SIGNALWIRE_SPACE_URL}/api/relay/rest/recordings/{recording_sid}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.delete(url, auth=_auth())
        if resp.status_code not in (200, 204, 404):
            resp.raise_for_status()
