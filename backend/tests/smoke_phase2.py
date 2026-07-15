"""Phase 2 end-to-end: recording ingestion, signed playback, token-gated stream,
and the transcription-gated retention sweep. Uses a dummy audio file (no real Twilio
download). Cleans up after itself.

Run against a server on :8899 →  ./.venv/Scripts/python.exe -m tests.smoke_phase2
"""

import asyncio
import base64
import hashlib
import hmac
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, select, text

from app.core.config import settings
from app.db import SessionLocal
from app.models import Call, Job, Number, Provider, Recording

BASE = "http://127.0.0.1:8899"
FWD = {"x-forwarded-proto": "https", "x-forwarded-host": "api.example.com"}
REC_WEBHOOK = "https://api.example.com/webhooks/twilio/recording"
REC_PATH = f"{BASE}/webhooks/twilio/recording"

CALL_SID = "CA_p2_test_0001"
REC_SID = "RE_p2_test_0001"
TO = "+13055559999"


def sign(url: str, params: dict[str, str]) -> str:
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    mac = hmac.new(settings.TWILIO_AUTH_TOKEN.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(mac).decode()


def check(name: str, cond: bool) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"phase2 smoke failed at: {name}")


async def cleanup() -> None:
    async with SessionLocal() as db:
        rec = (await db.execute(select(Recording).where(Recording.provider_recording_sid == REC_SID))).scalar_one_or_none()
        if rec and rec.storage_path and os.path.exists(rec.storage_path):
            os.remove(rec.storage_path)
        await db.execute(delete(Recording).where(Recording.provider_recording_sid == REC_SID))
        call = (await db.execute(select(Call).where(Call.provider_call_sid == CALL_SID))).scalar_one_or_none()
        if call:
            await db.execute(delete(Job).where(Job.type == "recording_fetch"))
            await db.execute(delete(Call).where(Call.id == call.id))
        await db.commit()


async def main() -> None:
    await cleanup()

    params = {
        "CallSid": CALL_SID, "RecordingSid": REC_SID,
        "RecordingUrl": "https://api.twilio.com/fake/RE123",
        "RecordingStatus": "completed", "RecordingDuration": "37",
    }

    with httpx.Client(timeout=10) as client:
        print("recording webhook:")
        r = client.post(REC_PATH, data=params, headers={**FWD, "X-Twilio-Signature": sign(REC_WEBHOOK, params)})
        check("recording webhook 200", r.status_code == 200)

    async with SessionLocal() as db:
        rec = (await db.execute(select(Recording).where(Recording.provider_recording_sid == REC_SID))).scalar_one()
        job = (await db.execute(select(Job).where(Job.type == "recording_fetch"))).scalars().all()
        check("recording row created", rec is not None and rec.duration_seconds == 37)
        check("bare call row auto-created (ordering-safe)",
              (await db.execute(select(Call).where(Call.provider_call_sid == CALL_SID))).scalar_one_or_none() is not None)
        check("recording_fetch job enqueued once", len(job) == 1 and job[0].payload["recording_id"] == str(rec.id))

        # Simulate a completed download (skip real Twilio fetch): write a dummy file.
        os.makedirs(settings.RECORDINGS_DIR, exist_ok=True)
        path = os.path.join(settings.RECORDINGS_DIR, f"{REC_SID}.mp3")
        payload = b"ID3\x03\x00fake-mp3-bytes"
        with open(path, "wb") as fh:
            fh.write(payload)
        rec.storage_path = path
        rec.downloaded_at = datetime.now(timezone.utc)
        await db.commit()
        rec_id = str(rec.id)

    with httpx.Client(timeout=10) as client:
        print("signed playback + stream:")
        r = client.post(f"{BASE}/api/auth/login", data={"username": "admin@example.com", "password": "admin-dev-pass"})
        token = r.json()["access_token"]

        check("play without auth -> 401", client.get(f"{BASE}/api/recordings/{rec_id}/play").status_code == 401)

        r = client.get(f"{BASE}/api/recordings/{rec_id}/play", headers={"Authorization": f"Bearer {token}"})
        check("play returns signed url", r.status_code == 200 and "/api/recordings/stream?token=" in r.json()["url"])
        stream_url = r.json()["url"]

        check("stream with bad token -> 403",
              client.get(f"{BASE}/api/recordings/stream?token=bogus").status_code == 403)

        r = client.get(f"{BASE}{stream_url}")
        check("stream returns audio bytes", r.status_code == 200
              and r.headers["content-type"] == "audio/mpeg" and r.content == payload)

    print("transcription-gated retention:")
    from app.worker import retention_sweep

    # Not transcribed yet + old -> must NOT delete.
    async with SessionLocal() as db:
        rec = await db.get(Recording, __import__("uuid").UUID(rec_id))
        rec.downloaded_at = datetime.now(timezone.utc) - timedelta(days=settings.RECORDING_RETENTION_DAYS + 5)
        rec.transcribed = False
        await db.commit()
    await retention_sweep()
    async with SessionLocal() as db:
        rec = await db.get(Recording, __import__("uuid").UUID(rec_id))
        check("un-transcribed old recording kept (gate holds)",
              rec.storage_path is not None and os.path.exists(rec.storage_path))

    # Now transcribed + old -> file removed, row + transcript flag kept.
    async with SessionLocal() as db:
        rec = await db.get(Recording, __import__("uuid").UUID(rec_id))
        rec.transcribed = True
        await db.commit()
    await retention_sweep()
    async with SessionLocal() as db:
        rec = await db.get(Recording, __import__("uuid").UUID(rec_id))
        check("transcribed old recording: file deleted, row kept",
              rec is not None and rec.storage_path is None and not os.path.exists(path))

    await cleanup()
    print("\nALL PHASE 2 CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit as e:
        print(e)
        sys.exit(1)
