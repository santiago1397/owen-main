"""What Retell tells OWEN after (and around) a call — `call_started`, `call_ended`, `call_analyzed`.

Retell posts `{"event": ..., "call": {...}}`. The route has already verified the signature;
this module maps `call.call_id` to OWEN's call through the persisted registry (so a webhook
after a restart still lands) and does three things, each ONCE however often Retell retries:

  * **call_ended** — the transcript (stored like owen_voice's, `transcriptions.engine =
    "retell"`, with the language when Retell names one), the version that actually answered
    (decision 14), the disconnection reason, and the REAL cost: one `call_charges` row of kind
    `ai.retell` carrying `call_cost.combined_cost` (cents) — which is what the spend cap counts
    (decision 11). Then the CRM is told: the existing `crm_report` -> `POST /api/events`
    "ended" event, `ai_call` extended per C4, under the call's own dedupe key so it merges
    with the report the runtime already sent.
  * **call_analyzed** — Retell's summary, sentiment and "successful", reported the same way.
  * **call_started** — logged and acknowledged; there is nothing to do with it.

## Once, and only once

`registry.claim_event` is a conditional UPDATE, so of two deliveries of the same event exactly
one proceeds. If processing then fails, the claim is RELEASED and the route answers 500 so
Retell's retry can do it — a claimed-but-unprocessed event would otherwise be lost for good.
The charge row is also idempotent on its own key (`uq_charge_leg_kind`).

An unknown call id is acknowledged (200) and ignored: a 4xx would make Retell retry something
that can never succeed, and it is how a call registered by another environment would look.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("integrations.retell.webhook")

KIND_EVENTS = {"call_ended": "ended", "call_analyzed": "analyzed"}
RATE_CODE = "retell.combined_cost"


def segments_from(call: dict) -> list[dict]:
    """Retell's `transcript_object` as OWEN's speaker-labelled segments — the shape
    `transcriptions.segments` and `crm_call.transcript_text` already use. PURE."""
    out: list[dict] = []
    for turn in call.get("transcript_object") or []:
        if not isinstance(turn, dict):
            continue
        text = str(turn.get("content") or "").strip()
        if not text:
            continue
        role = str(turn.get("role") or "").lower()
        out.append({"speaker": "agent" if role == "agent" else "caller", "text": text})
    if not out and str(call.get("transcript") or "").strip():
        # Only the flat string: keep it whole rather than guess at speakers.
        out.append({"speaker": "transcript", "text": str(call["transcript"]).strip()})
    return out


def cost_cents(call: dict):
    """`call_cost.combined_cost` (Retell reports cents), or None. PURE."""
    cost = call.get("call_cost") if isinstance(call.get("call_cost"), dict) else {}
    value = cost.get("combined_cost")
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def ended_fields(call: dict) -> dict:
    """The registry columns a `call_ended` fills. PURE."""
    out: dict = {}
    version = call.get("agent_version")
    if isinstance(version, int) and not isinstance(version, bool):
        out["retell_agent_version"] = version
    cents = cost_cents(call)
    if cents is not None:
        out["cost_cents"] = cents
    if call.get("disconnection_reason"):
        out["disconnection_reason"] = str(call["disconnection_reason"])[:200]
    return out


def analyzed_fields(call: dict) -> dict:
    """The registry columns a `call_analyzed` fills. PURE."""
    analysis = call.get("call_analysis") if isinstance(call.get("call_analysis"), dict) else {}
    out: dict = {}
    if analysis.get("call_summary"):
        out["summary"] = str(analysis["call_summary"])[:4000]
    if analysis.get("user_sentiment"):
        out["sentiment"] = str(analysis["user_sentiment"])[:40]
    if isinstance(analysis.get("call_successful"), bool):
        out["successful"] = analysis["call_successful"]
    return out


def ai_call_for(kind: str, snap: dict) -> dict:
    """C4's `ai_call` fields for one webhook's report. PURE. Only what is known goes in (the
    CRM merges records under one dedupe key, and an empty value would occupy the key)."""
    out: dict = {"engine": "retell", "retell_call_id": snap.get("retell_call_id")}
    if kind == "ended":
        for src, dst in (("retell_agent_version", "retell_agent_version"),
                         ("cost_cents", "cost_cents"),
                         ("disconnection_reason", "disconnection_reason")):
            if snap.get(src) is not None:
                out[dst] = snap[src]
        if snap.get("requests"):
            out["requests"] = list(snap["requests"])
        if snap.get("captured"):
            out["captured"] = dict(snap["captured"])
    else:
        for src, dst in (("summary", "summary"), ("sentiment", "sentiment"),
                         ("successful", "successful")):
            if snap.get(src) is not None:
                out[dst] = snap[src]
    return {k: v for k, v in out.items() if v not in (None, "", {}, [])}


async def persist_ended(snap: dict, call: dict) -> None:
    """Transcript + the cost row. Raises on a database failure (the claim is then released)."""
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.db import SessionLocal
    from app.models import Call, CallCharge, Transcription
    from app.services.ai_cost import KIND_AI_RETELL, PROVENANCE_RATED

    linkedid = str(snap.get("linkedid") or "")
    async with SessionLocal() as db:
        row = (await db.execute(
            select(Call).where(Call.provider_call_sid == linkedid).limit(1)
        )).scalar_one_or_none()
        if row is None:
            logger.warning("retell: no OWEN call for linkedid %s; transcript and cost kept "
                           "on the Retell row only", linkedid)
            return
        segments = segments_from(call)
        if segments:
            exists = (await db.execute(
                select(Transcription.id).where(Transcription.call_id == row.id,
                                               Transcription.engine == "retell").limit(1)
            )).scalar_one_or_none()
            if exists is None:
                from app.agents.crm_call import transcript_text

                db.add(Transcription(
                    call_id=row.id, recording_id=None, engine="retell",
                    text=transcript_text(segments),
                    # The language Retell reports, when it reports one. Unknown is NULL —
                    # never assumed English (the bug the owen_voice path had).
                    language=str(call.get("language") or "") or None,
                    segments=segments, status="completed",
                ))
        cents = cost_cents(call)
        if cents is not None:
            from decimal import Decimal

            av = snap.get("agent_version_id")
            import uuid as _uuid

            await db.execute(pg_insert(CallCharge).values(
                uniqueid=f"{linkedid}:retell:{snap.get('retell_call_id')}",
                linkedid=linkedid, kind=KIND_AI_RETELL, call_id=row.id,
                number_id=row.number_id, direction=row.direction or "inbound",
                started_at=row.started_at, rate_code=RATE_CODE,
                amount=(Decimal(str(cents)) / Decimal(100)).quantize(Decimal("0.000001")),
                provenance=PROVENANCE_RATED,
                agent_version_id=_uuid.UUID(str(av)) if av else None,
                usage={"call_cost": call.get("call_cost")},
            ).on_conflict_do_nothing(index_elements=["uniqueid", "kind"]))
        await db.commit()


async def report(kind: str, snap: dict, call: dict) -> bool:
    """One `ended` event to the CRM through the existing `crm_report` path."""
    from app.agents.crm_call import report_extra
    from app.integrations.crm import push as crm_push
    from app.integrations.crm.events import CallEventFacts
    from app.integrations.retell import functions

    linkedid = str(snap.get("linkedid") or "")
    # calls.id — the SAME id the runtime's own report used, so `dedupe_key` is the same and
    # the CRM merges this into that call row instead of filing a second one.
    owen_call_id = await functions._owen_call_id(linkedid)
    data: dict = {"ai_call_extra": ai_call_for(kind, snap)}
    if kind == "ended":
        segments = segments_from(call)
        if segments:
            data["transcript"] = segments
    extra = report_extra(agent_name=str(snap.get("agent_name") or ""), version=None,
                         outcome="", data=data, owen_call_id=owen_call_id)
    duration = None
    try:
        if call.get("duration_ms") is not None:
            duration = int(float(call["duration_ms"]) / 1000)
    except (TypeError, ValueError):
        duration = None
    return await crm_push.enqueue_call_event(CallEventFacts(
        phase="ended", owen_call_id=owen_call_id if owen_call_id != linkedid else "",
        linkedid=linkedid,
        caller_number=str(snap.get("caller_number") or ""),
        dialed_number=str(snap.get("dialed_number") or ""), direction="inbound",
        outcome="answered", duration_seconds=duration if kind == "ended" else None,
        extra=extra,
    ))


async def handle(body: dict, reg) -> tuple[int, dict]:
    """`(status_code, answer)` for one webhook delivery."""
    event = str(body.get("event") or "")
    call = body.get("call") if isinstance(body.get("call"), dict) else {}
    call_id = str(call.get("call_id") or "")
    if event == "call_started":
        logger.info("retell: call_started for %s", call_id or "?")
        return 200, {"ok": True}
    kind = KIND_EVENTS.get(event)
    if kind is None:
        return 200, {"ignored": f"event {event or '(none)'} is not one OWEN handles"}
    row = await reg.get(call_id) if call_id else None
    if row is None:
        logger.warning("retell: %s for a call OWEN did not register", event)
        return 200, {"ignored": "unknown call"}

    fields = ended_fields(call) if kind == "ended" else analyzed_fields(call)
    snap = await reg.claim_event(call_id, kind, fields)
    if snap is None:
        return 200, {"duplicate": True}
    try:
        if kind == "ended":
            await persist_ended(snap, call)
        await report(kind, snap, call)
    except Exception:  # noqa: BLE001 - release so Retell's retry can do it
        logger.exception("retell: processing %s for linkedid=%s failed; released for retry",
                         event, snap.get("linkedid"))
        try:
            await reg.release_event(call_id, kind)
        except Exception:  # noqa: BLE001
            logger.exception("retell: releasing %s failed", event)
        return 500, {"error": "processing failed; retry"}
    if kind == "ended":
        from app.agents import spend

        await spend.check_alert()
    return 200, {"ok": True}
