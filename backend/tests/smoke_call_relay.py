"""Completed-call → GHL relay handler, in-process (needs Postgres, not the HTTP server).

Drives handle_call_relay_ghl directly against a real DB, stubbing the outbound GHL POST
so nothing leaves the box:
- Enriched send: a call with analysis + transcript builds the full payload (attribution,
  new/returning, spam/category/summary via human-override precedence) and sets relayed_to_ghl.
- Idempotency: a second run skips (already relayed) and does not re-POST.
- Re-defer: a call with a recording but no analysis yet re-enqueues instead of sending,
  so answered calls wait for the AI analysis rather than relaying a bare payload.
Cleans up its own rows. Run: ./.venv/Scripts/python.exe -m tests.smoke_call_relay
"""

import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, text

from app.db import SessionLocal
from app.models import (
    Call,
    CallAnalysis,
    Caller,
    Campaign,
    Job,
    Number,
    Provider,
    Recording,
)
from app.providers import ghl_client
from app.workers import handlers

SID_ENRICHED = "CA_smoke_relay_enriched"
SID_DEFER = "CA_smoke_relay_defer"
FROM = "+13055557777"
TO = "+13055558888"


def check(name: str, cond: bool) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"smoke_call_relay failed at: {name}")


async def _provider(db, name: str) -> Provider:
    p = (await db.execute(select(Provider).where(Provider.name == name))).scalar_one_or_none()
    if not p:
        p = Provider(name=name)
        db.add(p)
        await db.flush()
    return p


async def cleanup() -> None:
    async with SessionLocal() as db:
        for sid in (SID_ENRICHED, SID_DEFER):
            call = (await db.execute(
                select(Call).where(Call.provider_call_sid == sid))).scalar_one_or_none()
            if call:
                await db.execute(delete(CallAnalysis).where(CallAnalysis.call_id == call.id))
                await db.execute(delete(Recording).where(Recording.call_id == call.id))
                await db.execute(text("DELETE FROM transcriptions WHERE call_id = :c"),
                                 {"c": str(call.id)})
                await db.execute(text("DELETE FROM jobs WHERE type='call_relay_ghl' "
                                      "AND payload->>'call_id' = :c"), {"c": str(call.id)})
                await db.execute(delete(Call).where(Call.id == call.id))
        await db.execute(delete(Caller).where(Caller.phone_number == FROM))
        await db.execute(delete(Number).where(Number.phone_number == TO))
        await db.execute(text("DELETE FROM campaigns WHERE name='Relay Campaign'"))
        await db.commit()


async def seed() -> tuple:
    async with SessionLocal() as db:
        prov = await _provider(db, "twilio")
        camp = Campaign(name="Relay Campaign", source="facebook")
        db.add(camp)
        await db.flush()
        num = Number(provider_id=prov.id, campaign_id=camp.id, phone_number=TO,
                     friendly_name="Relay Number", active=True)
        db.add(num)
        caller = Caller(phone_number=FROM, total_calls=3)
        db.add(caller)
        await db.flush()
        now = datetime.now(timezone.utc)

        enriched = Call(
            provider_id=prov.id, provider_call_sid=SID_ENRICHED, number_id=num.id,
            caller_id=caller.id, campaign_id=camp.id, direction="inbound",
            status="completed", status_rank=4, started_at=now, answered_at=now,
            ended_at=now, duration_seconds=42, is_new_for_campaign=True,
        )
        defer = Call(
            provider_id=prov.id, provider_call_sid=SID_DEFER, number_id=num.id,
            caller_id=caller.id, campaign_id=camp.id, direction="inbound",
            status="completed", status_rank=4, started_at=now, answered_at=now,
            ended_at=now, duration_seconds=17, is_new_for_campaign=False,
        )
        db.add_all([enriched, defer])
        await db.flush()

        # Enriched call: analysis with a human category override (must win over the model).
        db.add(CallAnalysis(
            call_id=enriched.id, is_spam=False, spam_confidence=0.1,
            category="lead", category_override="hot-lead", tags=["roofing", "quote"],
            summary="Caller wants a roof quote.", model="claude-haiku",
        ))
        # Defer call: a recording exists but analysis has NOT run yet.
        db.add(Recording(call_id=defer.id, provider_recording_sid="RE_smoke_defer",
                         status="completed", transcribed=False))
        await db.commit()
        return str(enriched.id), str(defer.id)


async def main() -> None:
    await cleanup()
    enriched_id, defer_id = await seed()

    # Stub the outbound POST so nothing leaves the box; capture the payload.
    sent: list[dict] = []

    async def fake_post(payload: dict) -> None:
        sent.append(payload)

    original = ghl_client.post_call_summary
    ghl_client.post_call_summary = fake_post
    try:
        print("enriched relay:")
        async with SessionLocal() as db:
            await handlers.handle_call_relay_ghl(db, {"call_id": enriched_id})
        check("POSTed exactly once", len(sent) == 1)
        p = sent[0]
        check("carries campaign attribution",
              p["campaign"] == "Relay Campaign" and p["campaign_source"] == "facebook")
        check("caller/number resolved", p["from"] == FROM and p["to"] == TO)
        check("new-for-campaign flag", p["is_new_for_campaign"] is True)
        check("analysis present", p["analysis"] is not None)
        check("human category override wins", p["analysis"]["category"] == "hot-lead")
        check("summary relayed", p["analysis"]["summary"] == "Caller wants a roof quote.")
        check("caller stats included", p["caller"]["total_calls"] == 3)
        check("not marked missed", p["missed"] is False and p["answered"] is True)

        async with SessionLocal() as db:
            row = (await db.execute(
                select(Call).where(Call.provider_call_sid == SID_ENRICHED))).scalar_one()
            check("relayed_to_ghl set", row.relayed_to_ghl is True and row.relayed_at is not None)

        print("idempotency:")
        async with SessionLocal() as db:
            await handlers.handle_call_relay_ghl(db, {"call_id": enriched_id})
        check("already-relayed call not re-POSTed", len(sent) == 1)

        print("re-defer while analysis pending:")
        async with SessionLocal() as db:
            await handlers.handle_call_relay_ghl(db, {"call_id": defer_id})
        check("no POST for un-analyzed call with recording", len(sent) == 1)
        async with SessionLocal() as db:
            defer_call = (await db.execute(
                select(Call).where(Call.provider_call_sid == SID_DEFER))).scalar_one()
            check("defer call NOT marked relayed", defer_call.relayed_to_ghl is False)
            requeued = (await db.execute(
                select(func.count()).select_from(Job)
                .where(Job.type == "call_relay_ghl",
                       Job.payload["call_id"].astext == defer_id))).scalar_one()
            check("relay re-enqueued for later", requeued >= 1)
    finally:
        ghl_client.post_call_summary = original

    await cleanup()
    print("\nALL CALL-RELAY CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit as e:
        print(e)
        sys.exit(1)
