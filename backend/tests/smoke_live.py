"""Live end-to-end smoke test against a running server + real Postgres.

Proves: auth works, webhook signatures are verified, and ingestion is idempotent,
out-of-order-safe, and event-sourced. Cleans up its own rows afterward.

Run:  ./.venv/Scripts/python.exe -m tests.smoke_live
"""

import asyncio
import base64
import hashlib
import hmac
import sys

import httpx
from sqlalchemy import delete, select, text

from app.core.config import settings
from app.db import SessionLocal
from app.models import Call, CallEvent, Caller, Campaign, Number, Provider

BASE = "http://127.0.0.1:8899"
WEBHOOK_URL = "https://api.example.com/webhooks/twilio/status"  # what Twilio "signs"
FWD = {"x-forwarded-proto": "https", "x-forwarded-host": "api.example.com"}

TEST_SID = "CA_smoke_test_0001"
TEST_FROM = "+13055551234"
TEST_TO = "+13055559999"


def sign(params: dict[str, str]) -> str:
    data = WEBHOOK_URL + "".join(f"{k}{params[k]}" for k in sorted(params))
    mac = hmac.new(settings.TWILIO_AUTH_TOKEN.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(mac).decode()


def check(name: str, cond: bool) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"smoke test failed at: {name}")


async def seed() -> None:
    async with SessionLocal() as db:
        prov = (await db.execute(select(Provider).where(Provider.name == "twilio"))).scalar_one_or_none()
        if not prov:
            prov = Provider(name="twilio", account_ref="ACxxxx")
            db.add(prov)
            await db.flush()
        camp = Campaign(name="Smoke Campaign", source="craigslist")
        db.add(camp)
        await db.flush()
        db.add(Number(provider_id=prov.id, campaign_id=camp.id, phone_number=TEST_TO,
                       friendly_name="Smoke Number", active=True))
        await db.commit()


async def cleanup() -> None:
    async with SessionLocal() as db:
        call = (await db.execute(select(Call).where(Call.provider_call_sid == TEST_SID))).scalar_one_or_none()
        if call:
            await db.execute(delete(CallEvent).where(CallEvent.call_id == call.id))
            await db.execute(delete(Call).where(Call.id == call.id))
        await db.execute(delete(Caller).where(Caller.phone_number == TEST_FROM))
        await db.execute(delete(Number).where(Number.phone_number == TEST_TO))
        await db.execute(text("DELETE FROM campaigns WHERE name='Smoke Campaign'"))
        await db.commit()


def post_status(client: httpx.Client, status: str, extra: dict | None = None) -> httpx.Response:
    params = {"CallSid": TEST_SID, "From": TEST_FROM, "To": TEST_TO,
              "Direction": "inbound", "CallStatus": status}
    if extra:
        params.update(extra)
    return client.post(WEBHOOK_URL_PATH, data=params,
                       headers={**FWD, "X-Twilio-Signature": sign(params)})


WEBHOOK_URL_PATH = f"{BASE}/webhooks/twilio/status"


async def main() -> None:
    await cleanup()
    await seed()

    with httpx.Client(timeout=10) as client:
        print("auth:")
        r = client.get(f"{BASE}/health")
        check("health 200", r.status_code == 200)

        r = client.post(f"{BASE}/api/auth/login",
                        data={"username": "admin@example.com", "password": "admin-dev-pass"})
        check("login 200", r.status_code == 200)
        token = r.json()["access_token"]

        r = client.get(f"{BASE}/api/auth/me")
        check("me without token -> 401", r.status_code == 401)
        r = client.get(f"{BASE}/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        check("me with token 200", r.status_code == 200 and r.json()["email"] == "admin@example.com")

        print("webhook signature:")
        bad = client.post(WEBHOOK_URL_PATH,
                          data={"CallSid": TEST_SID, "From": TEST_FROM, "To": TEST_TO, "CallStatus": "ringing"},
                          headers={**FWD, "X-Twilio-Signature": "wrong"})
        check("bad signature -> 403", bad.status_code == 403)

        print("ingestion (idempotent, out-of-order, event-sourced):")
        check("completed event 200", post_status(client, "completed", {"CallDuration": "42"}).status_code == 200)
        # out-of-order: a late 'ringing' must NOT regress the status
        check("late ringing 200", post_status(client, "ringing").status_code == 200)
        # duplicate of completed
        check("duplicate completed 200", post_status(client, "completed", {"CallDuration": "42"}).status_code == 200)

    async with SessionLocal() as db:
        call = (await db.execute(select(Call).where(Call.provider_call_sid == TEST_SID))).scalar_one()
        events = (await db.execute(select(CallEvent).where(CallEvent.call_id == call.id))).scalars().all()
        caller = (await db.execute(select(Caller).where(Caller.phone_number == TEST_FROM))).scalar_one()

        check("exactly one call row (idempotent)",
              len((await db.execute(select(Call).where(Call.provider_call_sid == TEST_SID))).scalars().all()) == 1)
        check("status stayed 'completed' (no regression)", call.status == "completed")
        check("duration captured", call.duration_seconds == 42)
        check("campaign stamped on call", call.campaign_id is not None)
        check("is_new_for_campaign = True", call.is_new_for_campaign is True)
        check("2 distinct events (completed + ringing, dupe deduped)", len(events) == 2)
        check("caller counted once", caller.total_calls == 1)

    await cleanup()
    print("\nALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit as e:
        print(e)
        sys.exit(1)
