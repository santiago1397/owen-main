"""Thin client for the owen-voice media service (AI_AGENT_SPEC D13).

Used for the operations that are ABOUT a session rather than the session itself: listing what
is live, and stopping one when a supervisor seizes the call. Running a conversation goes
through app/agents/remote.py instead, because that path has to satisfy the VoiceAgentSession
seam.

Every call is best-effort and returns a benign value on failure. Monitoring must degrade to
"we could not look it up" rather than raising into a live call path.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("telephony.voice_client")

_TIMEOUT = 10.0


def _headers() -> dict:
    from app.core.config import settings

    return {"X-OWEN-Voice-Key": settings.VOICE_SERVICE_KEY} if settings.VOICE_SERVICE_KEY else {}


async def active_sessions() -> list[dict]:
    """Live agent conversations. The agent's media leg was created inside owen-voice, so this
    is the only way OWEN can learn which channel to eject on take-over."""
    import httpx

    from app.core.config import settings

    base = (settings.VOICE_SERVICE_URL or "").rstrip("/")
    if not base:
        return []
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{base}/sessions/active", headers=_headers())
        if r.status_code >= 400:
            logger.warning("voice_client: /sessions/active -> %s", r.status_code)
            return []
        return list(r.json().get("sessions") or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice_client: /sessions/active failed: %r", exc)
        return []


async def session_for(linkedid: str) -> dict | None:
    for s in await active_sessions():
        if s.get("linkedid") == linkedid:
            return s
    return None


async def stop_session(linkedid: str, reason: str = "taken_over") -> bool:
    """End an agent session with an explicit PORT.

    Without this the take-over would eject the media leg and the session would end as a plain
    socket close reporting `default` — and the flow would follow its default edge, potentially
    playing voicemail at a caller who now has a human on the line."""
    import httpx

    from app.core.config import settings

    base = (settings.VOICE_SERVICE_URL or "").rstrip("/")
    if not base or not linkedid:
        return False
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                f"{base}/sessions/{linkedid}/stop",
                params={"reason": reason}, headers=_headers(),
            )
        return r.status_code < 400
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice_client: stop %s failed: %r", linkedid, exc)
        return False
