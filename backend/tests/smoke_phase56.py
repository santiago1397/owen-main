"""Phase 5 (manual overrides, settings) + Phase 6 (transcription + LLM analysis).

Drives the transcribe->analyze worker chain with the offline dummy engines, then checks
the API surfaces transcript/analysis, human overrides win, and the dashboard spam count
reflects overrides. Cleans up after itself.

Run against a server on :8899.
"""

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import delete, select, text

from app.core.config import settings
from app.db import SessionLocal
from app.models import (
    Call,
    CallAnalysis,
    Caller,
    Campaign,
    Number,
    Provider,
    Recording,
    Transcription,
)
from app.workers.handlers import handle_analyze, handle_transcribe

BASE = "http://127.0.0.1:8899"
CALL_SID = "CA_p56"
REC_SID = "RE_p56"
FROM = "+13055569999"
TO = "+13055560001"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"phase56 failed at: {name}")


async def cleanup():
    async with SessionLocal() as db:
        call = (await db.execute(select(Call).where(Call.provider_call_sid == CALL_SID))).scalar_one_or_none()
        if call:
            await db.execute(delete(CallAnalysis).where(CallAnalysis.call_id == call.id))
            await db.execute(delete(Transcription).where(Transcription.call_id == call.id))
            await db.execute(delete(Recording).where(Recording.call_id == call.id))
            await db.execute(delete(Call).where(Call.id == call.id))
        await db.execute(delete(Caller).where(Caller.phone_number == FROM))
        await db.execute(delete(Number).where(Number.phone_number == TO))
        await db.execute(text("DELETE FROM campaigns WHERE name='P56 Campaign'"))
        await db.commit()
    path = os.path.join(settings.RECORDINGS_DIR, f"{REC_SID}.mp3")
    if os.path.exists(path):
        os.remove(path)


async def seed() -> tuple[str, str, str]:
    async with SessionLocal() as db:
        prov = (await db.execute(select(Provider).where(Provider.name == "twilio"))).scalar_one_or_none()
        if not prov:
            prov = Provider(name="twilio"); db.add(prov); await db.flush()
        camp = Campaign(name="P56 Campaign", source="craigslist"); db.add(camp); await db.flush()
        num = Number(provider_id=prov.id, campaign_id=camp.id, phone_number=TO, active=True)
        db.add(num)
        caller = Caller(phone_number=FROM, total_calls=1); db.add(caller); await db.flush()
        call = Call(provider_id=prov.id, provider_call_sid=CALL_SID, caller_id=caller.id,
                    campaign_id=camp.id, status="completed", status_rank=4,
                    started_at=datetime.now(timezone.utc), duration_seconds=30,
                    is_new_for_campaign=True)
        db.add(call); await db.flush()
        os.makedirs(settings.RECORDINGS_DIR, exist_ok=True)
        path = os.path.join(settings.RECORDINGS_DIR, f"{REC_SID}.mp3")
        with open(path, "wb") as fh:
            fh.write(b"ID3fake")
        rec = Recording(call_id=call.id, provider_recording_sid=REC_SID, status="completed",
                        storage_path=path, downloaded_at=datetime.now(timezone.utc), transcribed=False)
        db.add(rec); await db.commit()
        return str(call.id), str(rec.id), str(caller.id)


async def main():
    await cleanup()
    call_id, rec_id, caller_id = await seed()

    print("phase 6 — transcribe -> analyze (dummy engines):")
    async with SessionLocal() as db:
        await handle_transcribe(db, {"recording_id": rec_id})
    async with SessionLocal() as db:
        t = (await db.execute(select(Transcription).where(Transcription.call_id == uuid.UUID(call_id)))).scalar_one()
        rec = await db.get(Recording, uuid.UUID(rec_id))
        check("transcription row created", bool(t.text))
        check("recording marked transcribed (retention now permitted)", rec.transcribed is True)

    async with SessionLocal() as db:
        await handle_analyze(db, {"call_id": call_id})
    async with SessionLocal() as db:
        a = (await db.execute(select(CallAnalysis).where(CallAnalysis.call_id == uuid.UUID(call_id)))).scalar_one()
        caller = await db.get(Caller, uuid.UUID(caller_id))
        check("analysis detected spam from transcript content", a.is_spam is True)
        check("category = sales-spam", a.category == "sales-spam")
        check("caller.spam_score updated", caller.spam_score is not None)

    with httpx.Client(timeout=10) as c:
        token = c.post(f"{BASE}/api/auth/login",
                       data={"username": "admin@example.com", "password": "admin-dev-pass"}).json()["access_token"]
        H = {"Authorization": f"Bearer {token}"}

        print("phase 6 — read API surfaces transcript + analysis:")
        d = c.get(f"{BASE}/api/calls/{call_id}", headers=H).json()
        check("call detail includes transcript", bool(d["transcript"]))
        check("call detail includes analysis, is_spam=True", d["analysis"]["is_spam"] is True)
        check("list shows effective is_spam=True",
              next(i for i in c.get(f"{BASE}/api/calls", headers=H).json()["items"]
                   if i["id"] == call_id)["is_spam"] is True)

        print("dashboard spam count (pre-override):")
        s = c.get(f"{BASE}/api/dashboard/summary", params={"range": "7d"}, headers=H).json()
        check("dashboard spam_calls >= 1", s["spam_calls"] >= 1)

        print("phase 5 — human overrides win:")
        r = c.patch(f"{BASE}/api/calls/{call_id}/analysis", headers=H,
                    json={"is_spam_override": False, "category_override": "support"})
        check("override accepted", r.status_code == 200)
        d = c.get(f"{BASE}/api/calls/{call_id}", headers=H).json()
        check("effective is_spam now False (override wins)", d["is_spam"] is False)
        check("effective category now 'support'", d["category"] == "support")
        s2 = c.get(f"{BASE}/api/dashboard/summary", params={"range": "7d"}, headers=H).json()
        check("dashboard spam_calls dropped after override", s2["spam_calls"] == s["spam_calls"] - 1)

        print("phase 5 — manual caller label + settings:")
        r = c.patch(f"{BASE}/api/callers/{caller_id}", headers=H, json={"label": "known-spam"})
        check("caller label set", r.status_code == 200 and r.json()["label"] == "known-spam")

        st = c.get(f"{BASE}/api/settings", headers=H).json()
        check("settings expose twilio webhook url",
              st["providers"]["twilio"]["status_webhook"].endswith("/webhooks/twilio/status"))
        check("settings expose active engines", st["engines"]["transcription"] == "dummy")

    await cleanup()
    print("\nALL PHASE 5+6 CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit as e:
        print(e); sys.exit(1)
