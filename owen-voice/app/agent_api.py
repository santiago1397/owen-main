"""`POST /sessions` — the seam OWEN's `ai_agent` flow node calls (step 3, AI_AGENT_SPEC D13).

CONTRACT: the request BLOCKS for the whole conversation and returns `{port, data}` — exactly
the shape `RunAgentFn` already expects in app/flows/runtime.py, so the flow interpreter needs
no structural change. Its failure mode is already correct: if this service restarts mid-call
the request fails, `_h_ai_agent` catches it, returns `failed`, and the flow routes to
`default_fallback` (voicemail). The caller gets voicemail rather than dead air.

The caller's channel belongs to OWEN's Stasis app; the external-media channel we create
belongs to ours. An ARI bridge does not care — it mixes channels regardless of which app
owns them — and while the `ai_agent` node is running, OWEN's interpreter is parked awaiting
this call and is not touching the channel.

CONCURRENCY (D10): a fixed number of simultaneous sessions, and a refusal is IMMEDIATE.
Blocking until a slot frees would leave the caller listening to silence — the exact failure
the whole design exists to prevent. A fast 503 becomes `failed` -> `default_fallback`, which
is a path that already works.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.session import MediaSession, registry

logger = logging.getLogger("voice.agent_api")

router = APIRouter(prefix="/sessions", tags=["agent"])

# One semaphore for the whole process. `locked()` is checked before acquiring so a full pool
# refuses instantly instead of queueing.
_slots = asyncio.Semaphore(settings.MAX_SESSIONS)
_active = 0


class AgentConfig(BaseModel):
    """The pinned agent-version config, flattened. Mirrors the backend's AgentSpec so the
    two ends stay recognisably the same object."""

    persona: str = ""
    greeting: str = ""
    voice: str = ""
    model: str = ""
    llm_base_url: str = ""
    knowledge: str = ""
    tools: dict = Field(default_factory=dict)
    max_call_seconds: Optional[int] = None
    max_silence_seconds: Optional[int] = None
    half_duplex: Optional[bool] = None
    tts_instructions: str = ""


class SessionIn(BaseModel):
    channel_id: str                 # the LIVE caller channel, already in OWEN's Stasis app
    linkedid: str = ""
    caller_number: str = ""
    dialed_number: str = ""
    agent: AgentConfig = Field(default_factory=AgentConfig)


class SessionOut(BaseModel):
    port: str                       # transfer | end_call | default | failed
    data: dict = Field(default_factory=dict)
    turns: int = 0
    transcript: list = Field(default_factory=list)
    session_uuid: str = ""


def _auth(key: Optional[str]) -> None:
    """Shared secret. This service is loopback-published and unroutable from outside, so the
    secret is defence in depth rather than the boundary — but an unauthenticated endpoint that
    can seize a live call is not something to leave lying around either."""
    expected = settings.SERVICE_KEY
    if expected and key != expected:
        raise HTTPException(401, "bad or missing X-OWEN-Voice-Key")


@router.post("", response_model=SessionOut)
async def run_session(
    body: SessionIn,
    x_owen_voice_key: Optional[str] = Header(default=None),
) -> SessionOut:
    _auth(x_owen_voice_key)

    global _active
    if _slots.locked() or _active >= settings.MAX_SESSIONS:
        # Fast refusal, not a queue. See the module docstring.
        logger.warning("sessions: at capacity (%d), refusing linkedid=%s",
                       settings.MAX_SESSIONS, body.linkedid)
        raise HTTPException(503, "voice agent capacity reached")

    await _slots.acquire()
    _active += 1
    session = registry.create(label=f"agent:{body.linkedid or body.channel_id}")
    session.mode = "agent"
    session.call_channel_id = body.channel_id
    session.linkedid = body.linkedid
    session.caller_number = body.caller_number
    session.agent = body.agent.model_dump()
    if body.agent.half_duplex is not None:
        session.half_duplex = body.agent.half_duplex
    if body.agent.voice:
        session.tts_voice = body.agent.voice
    if body.agent.tts_instructions:
        session.tts_instructions = body.agent.tts_instructions

    # Imported here to avoid a circular import at module load (main owns the ARI client).
    from app.main import attach_media_to_call, teardown_session

    try:
        ok = await attach_media_to_call(session)
        if not ok:
            logger.error("sessions: could not attach media for linkedid=%s", body.linkedid)
            return SessionOut(port="failed", session_uuid=session.session_uuid)

        # Block until the conversation ends: the caller hangs up, a guardrail fires, or the
        # agent takes an exit tool. `done` is set by the connection handler in every case.
        timeout = (body.agent.max_call_seconds or settings.AGENT_MAX_CALL_SECONDS) + 30
        try:
            await asyncio.wait_for(session.done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            # Belt and braces: the guardrails should have ended this long before.
            logger.warning("sessions: hard timeout for linkedid=%s", body.linkedid)
            session.result_port = "end_call"

        return SessionOut(
            port=session.result_port or "default",
            data=session.result_data,
            turns=session.turns,
            transcript=session.transcript,
            session_uuid=session.session_uuid,
        )
    except Exception:  # noqa: BLE001 - never raise into the flow; `failed` routes to fallback
        logger.exception("sessions: run failed for linkedid=%s", body.linkedid)
        return SessionOut(port="failed", session_uuid=session.session_uuid)
    finally:
        await teardown_session(session)
        _active -= 1
        _slots.release()
        registry.prune()


@router.get("/capacity")
async def capacity() -> dict:
    return {
        "max_sessions": settings.MAX_SESSIONS,
        "active": _active,
        "available": max(0, settings.MAX_SESSIONS - _active),
    }
