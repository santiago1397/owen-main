"""OpenPhone REST client — STRICTLY READ-ONLY (docs/GHL_SYNC_SPEC.md D11 + D16).

OpenPhone is the account the team makes OUTBOUND customer calls from. OWEN reads those call
logs so each becomes a recorded TOUCH on an existing lead (time-to-first-callback,
touches-before-close, leads never called back) — they are NEVER counted as leads themselves.

════════════════════════════════════════════════════════════════════════════════════════
  HARD CONSTRAINT — owner-mandated, do not relax without an explicit decision.

  This module issues **GET requests only**. It must never send a message, place a call,
  or write a contact. In OpenPhone those are billable actions against a real phone
  number: a stray POST /messages does not "fail a test", it TEXTS A REAL CUSTOMER and
  charges for it.

  The guarantee is STRUCTURAL, not a matter of care:
    - the only transport helper is `_get`; there is no `_post`/`_put`/`_delete`;
    - `_get` asserts the path is a read path before the request leaves the process.
  Adding a write method here silently removes the guarantee. Don't. If a write is ever
  genuinely needed, it belongs in a separate, separately-reviewed module.
════════════════════════════════════════════════════════════════════════════════════════

UNVERIFIED: the endpoint paths, auth header form and response shapes below are from
documented behaviour, NOT yet confirmed against the live account. `app.scripts.probe_openphone`
exists to confirm them safely (it only calls the functions here). Treat every shape as a
hypothesis until that probe has run — see the spec's open items.
"""

import logging
from typing import Any, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger("openphone_client")

# Wall-clock ceiling per request. Reads are small; a slow OpenPhone must not wedge a worker.
_TIMEOUT = 20.0


def _headers() -> dict:
    """OpenPhone takes the raw API key in `Authorization` — NOT a `Bearer <key>` form.
    (A `Bearer` prefix is the usual cause of a 401 here.)"""
    return {
        "Authorization": settings.OPENPHONE_API_KEY,
        "Accept": "application/json",
    }


async def _get(path: str, params: Optional[dict] = None) -> Any:
    """The ONLY transport in this module. GET-only by construction.

    Raises RuntimeError when unconfigured (rather than firing an unauthenticated request),
    and raises on non-2xx so callers surface the failure instead of treating it as empty."""
    if not settings.openphone_enabled:
        raise RuntimeError("OpenPhone is not configured (OPENPHONE_API_KEY is empty)")

    # Belt-and-braces against a future edit smuggling a write path through this helper.
    # Read paths are plain resource paths; anything action-shaped is refused outright.
    lowered = path.lower()
    if any(verb in lowered for verb in ("/send", "/call/", "/dial", "/create")):
        raise RuntimeError(
            f"refusing non-read OpenPhone path {path!r} — this client is read-only (D16)"
        )

    url = f"{settings.OPENPHONE_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, params=params or None, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def list_phone_numbers() -> list[dict]:
    """GET /phone-numbers — the numbers on the account. Cheapest possible connectivity check,
    which is why the probe calls it first."""
    body = await _get("/phone-numbers")
    return body.get("data", body if isinstance(body, list) else [])


async def list_calls_with(
    phone_number_id: str, participant: str, *,
    page_token: Optional[str] = None, limit: int = 50,
) -> dict:
    """GET /calls — calls between our OpenPhone number and ONE participant.

    `participant` is MANDATORY and that is an API constraint, not a design choice: OpenPhone
    rejects `/calls` without it (HTTP 400), including when a `since`/`createdAfter` filter is
    supplied. **There is no time-based sweep** — you cannot ask for "all calls since X".
    Verified against the live account 2026-07-24; see spec D11a.

    This is why OpenPhone ingestion is contact-driven: OWEN iterates the callers it cares
    about and asks about each. Costs one request per contact, against a 10 req/s limit — so
    the caller must scope the poll set rather than sweeping every known caller.

    Returns the raw page: `{"data": [...], "totalItems": n, "nextPageToken": ...}`."""
    params: dict = {
        "phoneNumberId": phone_number_id,
        "participants[]": participant,
        "maxResults": limit,
    }
    if page_token:
        params["pageToken"] = page_token
    return await _get("/calls", params)


async def get_call(call_id: str) -> dict:
    """GET /calls/{id} — one call's detail."""
    body = await _get(f"/calls/{call_id}")
    return body.get("data", body)


async def get_call_transcript(call_id: str) -> dict:
    """GET /call-transcripts/{id} — speaker-labeled transcript.

    `dialogue[]` entries are `{identifier, start, end, content}` where `identifier` is the
    participant's phone number — i.e. already diarized. This maps onto OWEN's existing
    `transcriptions.segments` shape with no new schema, and costs no STT: OpenPhone has
    already transcribed it."""
    body = await _get(f"/call-transcripts/{call_id}")
    return body.get("data", body)


async def get_call_recording(call_id: str) -> dict:
    """GET /call-recordings/{id} — `{url, type: audio/mpeg, duration, status}`. The URL is
    hosted on share.quo.com."""
    body = await _get(f"/call-recordings/{call_id}")
    return body.get("data", body)


async def get_call_summary(call_id: str) -> dict:
    """GET /call-summaries/{id} — `{summary, nextSteps, jobs, status}`.

    CAUTION: verified to return 200 with this schema, but all three content fields came back
    EMPTY on the sampled call (it hit a voicemail greeting, so there was no conversation).
    Whether they populate on real dialogue is unconfirmed — do not design against `jobs`
    until a richer sample proves it out. See spec D11a."""
    body = await _get(f"/call-summaries/{call_id}")
    return body.get("data", body)


async def list_contacts(page_token: Optional[str] = None, limit: int = 50) -> dict:
    """GET /contacts — the account's contacts, for matching against OWEN callers by phone.
    Each carries `defaultFields{firstName,lastName,phoneNumbers[],emails[]}` plus
    `customFields`, `externalId` and `source`."""
    params: dict = {"maxResults": limit}
    if page_token:
        params["pageToken"] = page_token
    return await _get("/contacts", params)
