"""`owen_voice` engine — runs a real conversation in the owen-voice media service (step 3).

This is the third entry in the engine registry alongside `dummy` and the (now superseded)
`openai_realtime`, and it exists because the audio pipeline deliberately does NOT live in this
process: the worker is capped at 0.5 CPU / 256M and also runs the ARI consumer every live call
depends on, so audio work here would contend with — and could take down — call ingestion
platform-wide (AI_AGENT_SPEC D2).

The seam is unchanged. `VoiceAgentSession.run(spec, ctx) -> AgentResult{port, data}` is what
the flow interpreter already calls; this implementation just does the work over HTTP instead
of in-process. One blocking request per call (D13), which maps 1:1 onto `RunAgentFn` and needs
no interpreter change.

FAILURE CONTRACT, unchanged from every other engine: any failure — service down, at capacity,
timeout, malformed reply — returns the `failed` port so `_h_ai_agent` routes to the flow's
`default_fallback` (voicemail). A caller never hears dead air because this service is
unavailable. A 503 from the capacity gate is deliberately treated the same way: the spec's
answer to "all agent slots are busy" IS the flow fallback.
"""

from __future__ import annotations

import logging

from app.agents.session import AgentCallContext, AgentResult, AgentSpec

logger = logging.getLogger("agents.remote")

ENGINE_NAME = "owen_voice"


class RemoteVoiceAgentSession:
    """Delegates one call's conversation to the owen-voice service."""

    name = ENGINE_NAME

    async def run(self, spec: AgentSpec, ctx: AgentCallContext) -> AgentResult:
        # Lazy imports keep this module importable in the dependency-light sandbox, the same
        # discipline the rest of app/agents follows.
        import httpx

        from app.core.config import settings

        base = (settings.VOICE_SERVICE_URL or "").rstrip("/")
        if not base:
            logger.warning("owen_voice: VOICE_SERVICE_URL not set; taking the failed port")
            return AgentResult(port="failed")

        guard = spec.guardrails if isinstance(spec.guardrails, dict) else {}
        payload = {
            "channel_id": ctx.channel_id,
            "linkedid": ctx.linkedid,
            "caller_number": ctx.caller_number or "",
            "agent": {
                "persona": spec.persona,
                "greeting": spec.greeting,
                "voice": spec.voice,
                "model": spec.model,
                "llm_base_url": str(spec.config.get("llm_base_url") or ""),
                "knowledge": spec.knowledge,
                "tools": spec.tools or {},
                "max_call_seconds": guard.get("max_call_seconds"),
                "max_silence_seconds": guard.get("max_silence_seconds"),
                "tts_instructions": str(spec.config.get("tts_instructions") or ""),
                # Only the NAMES travel; the targets stay here and are resolved
                # against the pinned version when the agent picks one (D9).
                # Declared verbatim: the model sees names and schemas, and the URL
                # set is fixed in the pinned version an operator wrote (D6).
                "custom_tools": spec.config.get("custom_tools") or [],
                "transfer_targets": {
                    k: {"kind": (v or {}).get("kind", "number")}
                    for k, v in (spec.config.get("transfer_targets") or {}).items()
                    if isinstance(v, dict)
                },
            },
        }
        headers = {}
        if settings.VOICE_SERVICE_KEY:
            headers["X-OWEN-Voice-Key"] = settings.VOICE_SERVICE_KEY

        # The request is held open for the WHOLE conversation, so the read timeout is a
        # backstop far above any real call rather than a latency budget. The service enforces
        # the actual limits via the agent's own guardrails.
        timeout = httpx.Timeout(
            float(settings.VOICE_SERVICE_TIMEOUT_SECONDS), connect=5.0
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{base}/sessions", json=payload, headers=headers)
        except Exception as exc:  # noqa: BLE001 - unreachable service -> fallback, not dead air
            logger.warning("owen_voice: request failed (%s); taking the failed port", exc)
            return AgentResult(port="failed")

        if resp.status_code == 503:
            # At capacity (D10). This is an expected, designed-for outcome, not an incident:
            # the flow's fallback (voicemail / ring operators) is the correct response, and it
            # already works. Logged at WARNING so sustained capacity pressure is visible in
            # /api/ai/errors rather than silent.
            logger.warning(
                "owen_voice: agent capacity reached for linkedid=%s; routing to fallback",
                ctx.linkedid,
            )
            return AgentResult(port="failed")
        if resp.status_code >= 400:
            logger.warning("owen_voice: %s %s", resp.status_code, resp.text[:200])
            return AgentResult(port="failed")

        try:
            data = resp.json()
        except ValueError:
            logger.warning("owen_voice: non-JSON response; taking the failed port")
            return AgentResult(port="failed")

        port = str(data.get("port") or "default")
        result = data.get("data") if isinstance(data.get("data"), dict) else {}
        # The transcript rides back on the result so a later ticket can persist it without a
        # second round trip. Step 5 writes it to `transcriptions`; here it is simply carried.
        transcript = data.get("transcript")
        if transcript:
            result = dict(result)
            result["transcript"] = transcript
            result["turns"] = data.get("turns") or 0
        logger.info(
            "owen_voice: linkedid=%s finished on port '%s' after %s turn(s)",
            ctx.linkedid, port, data.get("turns") or 0,
        )
        return AgentResult(port=port, data=result)
