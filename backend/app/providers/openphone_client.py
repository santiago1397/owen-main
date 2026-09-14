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

QUERY STRINGS — VERIFIED AGAINST QUO'S API REFERENCE (2026-09-14, after a live 400):
array parameters are sent as a REPEATED KEY WITHOUT BRACKETS. Quo's reference says so
verbatim for both list endpoints: "Repeat the 'participants' key for each phone number
without brackets, e.g. 'participants=%2B15555555555&participants=%2B15555555556'"
(List messages) and "Pass the 'participants' key without brackets" (List calls). This client
used to send `participants[]`, and `GET /messages` answered 400 on production for every
participant. httpx renders a str or a list value under a plain key as exactly that form; the
exact bytes are pinned by `tests/test_openphone_query.py`. Participants must be E.164.

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
        # No brackets (see the module header). List calls: required, max ONE item, E.164.
        "participants": participant,
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


def pick_recording(body: Any) -> dict:
    """The ONE recording in a `/call-recordings/{id}` response, or `{}` when there is none.

    MEASURED ON PRODUCTION 2026-09-14: `data` is a LIST, one entry per recording —

        with audio:    {"data": [{"duration", "id", "startTime", "status", "type", "url"}]}
        without audio: {"data": []}

    This function used to return that list as though it were one dict, and both callers
    did `.get("url")` on it: the poll swallowed the AttributeError and marked every call
    as having no recording, and the stream endpoint would have answered 500. A bare dict
    (the shape the old docstring assumed) is still accepted, defensively.

    Several recordings: a `completed` one with a url wins, then the longest with a url.
    Anything that is not a dict with a non-empty `url` is not a recording.
    """
    data = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return {}
    playable = [r for r in data
                if isinstance(r, dict) and isinstance(r.get("url"), str) and r["url"].strip()]
    if not playable:
        return {}

    def rank(rec: dict) -> tuple[int, int]:
        try:
            duration = int(rec.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        return (1 if str(rec.get("status") or "").lower() == "completed" else 0, duration)

    return max(playable, key=rank)


async def get_call_recording(call_id: str) -> dict:
    """GET /call-recordings/{id} — the call's recording as ONE dict
    `{url, type, duration, status, ...}`, or `{}` when the call has no audio.

    Always a dict, never the raw list Quo sends: see `pick_recording`. The URL is hosted on
    share.quo.com. Raises (like every read here) when the request itself fails, so a caller
    can tell "no recording" (`{}`) from "could not ask" (an exception)."""
    return pick_recording(await _get(f"/call-recordings/{call_id}"))


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


# ══════════════════════════════════════════════════════════════════════════════════════════
#  ADDED 2026-09-11 for the CRM mirror (app/integrations/openphone/).
#
#  Every function below is a GET through `_get`, for the same reason the rest of this module
#  is: the mirror's whole job is to COPY OpenPhone activity into the CRM, and a mirror that
#  can write is a mirror that can text a customer from a number the business is migrating
#  away from. There is still no `_post`. Adding one removes the guarantee for the whole file.
#
#  UNVERIFIED, in the same sense as the header's warning and for the same reason: the probe
#  (`app.scripts.probe_openphone`) has confirmed `/phone-numbers`, `/calls`, `/contacts`,
#  `/call-recordings`, `/call-transcripts` and `/call-summaries` against the live account.
#  It has NOT yet confirmed `/messages` or `/conversations`. The probe has been extended to
#  cover both; until it is run with the production key, treat these two shapes as a
#  hypothesis and expect `sync.py` to degrade rather than to be right.
# ══════════════════════════════════════════════════════════════════════════════════════════


async def list_messages(
    phone_number_id: str, participant: str, *,
    page_token: Optional[str] = None, limit: int = 50,
) -> dict:
    """GET /messages — texts between our OpenPhone number and ONE participant.

    Shaped deliberately like `list_calls_with`, because the API constraint is expected to be
    the same one: `/calls` rejects a participant-less query with a 400 (verified, spec D11a),
    and `/messages` is documented with the same required `participants` array. Assuming the
    symmetry is what lets `sync.py` drive calls and texts from one participant loop.

    If it turns out `/messages` DOES accept a time-only sweep, that is strictly good news and
    the fix is in `sync.py`'s enumeration, not here.

    Returns the raw page: `{"data": [...], "totalItems": n, "nextPageToken": ...}`.
    """
    params: dict = {
        "phoneNumberId": phone_number_id,
        # No brackets (see the module header) — `participants[]` is what 400'd on
        # production. List messages: required, up to 10 items, E.164; one here, because
        # several participants means a GROUP conversation, not this customer's thread.
        "participants": participant,
        "maxResults": limit,
    }
    if page_token:
        params["pageToken"] = page_token
    return await _get("/messages", params)


async def list_conversations(
    phone_number_id: str, *, page_token: Optional[str] = None, limit: int = 50,
) -> dict:
    """GET /conversations — the threads on our number, most-recently-active first.

    THIS IS THE ENUMERATOR, and it is the one thing that makes a 30-day backfill possible.
    Spec D11a established the hard constraint: there is NO time-based sweep of `/calls`, so
    OWEN cannot ask "everything that happened since X" — it can only ask "what happened with
    THIS participant". That is fine for D11 (touches on known leads) and useless for a
    mirror, which must not silently omit the strangers.

    `/conversations` closes that gap if it exists: it lists threads with their participants
    and `lastActivityAt`, which turns "who do I ask about?" into a query rather than a guess.
    `sync.py` pages it until `lastActivityAt` falls out of the window, then asks `/calls` and
    `/messages` about each participant it found.

    UNVERIFIED — see the block comment above. `sync.participants_in_window` treats a failure
    here as "enumerate from the sources that ARE verified" (OpenPhone `/contacts` plus the
    CRM's own contacts), so an account where this endpoint does not exist mirrors a narrower
    set rather than mirroring nothing.
    """
    # Quo's reference names the filter `phoneNumbers` (array, E.164 or a `PN` id, 1-100
    # items). There is NO `phoneNumberId` parameter on this endpoint; this client used to
    # send one. Encoded like the other arrays: a plain repeated key, no brackets.
    params: dict = {"phoneNumbers": phone_number_id, "maxResults": limit}
    if page_token:
        params["pageToken"] = page_token
    return await _get("/conversations", params)


async def fetch_recording_bytes(url: str) -> tuple[bytes, str]:
    """GET a recording's media from the URL `get_call_recording` handed back.

    NOT an api.openphone.com call: the URL is a share.quo.com link the API just gave us, and
    it carries its own authorisation, so **the API key is deliberately not sent here** — it
    would be handing a credential to a host that did not ask for it.

    Returns `(bytes, content_type)`. Raises on a non-2xx, like `_get`, so the route above it
    can answer honestly instead of streaming an error page as audio.
    """
    if not str(url or "").strip():
        raise RuntimeError("no recording URL to fetch")
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content, resp.headers.get("content-type", "audio/mpeg")
