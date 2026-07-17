"""Inbound SMS → ingest → GHL-relay-enqueue end-to-end.

- Seeds a campaign+number on SignalWire.
- POSTs a signature-verified cXML SMS to /webhooks/signalwire/message; asserts a Message
  row is created with campaign attribution and a `message_relay_ghl` Job is enqueued.
- Re-POSTs the same MessageSid to assert idempotency (no duplicate row / no re-enqueue
  beyond the retry semantics).
- Also unit-checks the adapter's parse_message_event (incl. _tracking_number override).
Cleans up after itself. Run against a server on :8899.
"""

import asyncio
import base64
import hashlib
import hmac
import sys

import httpx
from sqlalchemy import delete, func, select, text

from app.core.config import settings
from app.db import SessionLocal
from app.models import Caller, Campaign, Job, Message, Number, Provider
from app.providers.signalwire import SignalWireAdapter

BASE = "http://127.0.0.1:8899"
FWD = {"x-forwarded-proto": "https", "x-forwarded-host": "api.example.com"}

MSG_URL = "https://api.example.com/webhooks/signalwire/message"
MSG_PATH = f"{BASE}/webhooks/signalwire/message"

TO_SW = "+13055540003"      # tracking number on SignalWire
FROM = "+13055543333"
SID = "SM_smoke_msg"


def sign(secret: str, url: str, params: dict) -> str:
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(secret.encode(), data.encode(), hashlib.sha1).digest()).decode()


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"smoke_messages failed at: {name}")


async def cleanup():
    async with SessionLocal() as db:
        # Drop relay jobs pointing at our message rows before deleting the messages.
        msg_ids = (await db.execute(
            select(Message.id).where(Message.provider_message_sid == SID))).scalars().all()
        for mid in msg_ids:
            await db.execute(text("DELETE FROM jobs WHERE type='message_relay_ghl' "
                                  "AND payload->>'message_id' = :mid"), {"mid": str(mid)})
        await db.execute(delete(Message).where(Message.provider_message_sid == SID))
        await db.execute(delete(Caller).where(Caller.phone_number == FROM))
        await db.execute(delete(Number).where(Number.phone_number == TO_SW))
        await db.execute(text("DELETE FROM campaigns WHERE name='MSG Campaign'"))
        await db.commit()


async def _provider(db, name):
    p = (await db.execute(select(Provider).where(Provider.name == name))).scalar_one_or_none()
    if not p:
        p = Provider(name=name); db.add(p); await db.flush()
    return p


async def seed():
    async with SessionLocal() as db:
        sw = await _provider(db, "signalwire")
        camp = Campaign(name="MSG Campaign", source="craigslist")
        db.add(camp); await db.flush()
        db.add(Number(provider_id=sw.id, campaign_id=camp.id, phone_number=TO_SW,
                      friendly_name="MSG SignalWire", active=True))
        await db.commit()


def sms_params(sid, frm, to, body):
    return {"MessageSid": sid, "From": frm, "To": to, "Body": body,
            "MessageStatus": "received", "NumMedia": "0"}


def test_parse():
    print("adapter parse_message_event:")
    adapter = SignalWireAdapter()
    p = sms_params(SID, FROM, TO_SW, "hello world")
    evt = adapter.parse_message_event(p)
    check("parses sid/from/to/body",
          evt.provider_message_sid == SID and evt.from_number == FROM
          and evt.to_number == TO_SW and evt.body == "hello world")
    # tracking-number query override wins over payload To
    p2 = dict(p, To="+19999999999", _tracking_number=TO_SW)
    evt2 = adapter.parse_message_event(p2)
    check("_tracking_number overrides payload To", evt2.to_number == TO_SW)
    # MMS media collection
    p3 = dict(p, NumMedia="2", MediaUrl0="http://m/0.jpg", MediaUrl1="http://m/1.jpg")
    evt3 = adapter.parse_message_event(p3)
    check("collects MMS media urls",
          evt3.num_media == 2 and evt3.media_urls == ["http://m/0.jpg", "http://m/1.jpg"])


async def main():
    await cleanup(); await seed()
    test_parse()

    with httpx.Client(timeout=10) as c:
        print("webhook signature + ingest:")
        p = sms_params(SID, FROM, TO_SW, "quote on the truck?")

        bad = c.post(MSG_PATH, data=p, headers={**FWD, "X-SignalWire-Signature":
                     sign(settings.TWILIO_AUTH_TOKEN, MSG_URL, p)})  # wrong key
        check("rejects twilio-key signature -> 403", bad.status_code == 403)

        good = c.post(MSG_PATH, data=p, headers={**FWD, "X-SignalWire-Signature":
                      sign(settings.SIGNALWIRE_AUTH_TOKEN, MSG_URL, p)})
        check("accepts signalwire-key signature -> 200", good.status_code == 200)

    async with SessionLocal() as db:
        msg = (await db.execute(
            select(Message).where(Message.provider_message_sid == SID))).scalar_one()
        check("message row created", msg is not None)
        check("message attributed to campaign", msg.campaign_id is not None)
        check("message body stored", msg.body == "quote on the truck?")
        check("message from/to correct", msg.from_number == FROM and msg.to_number == TO_SW)
        check("caller upserted from sender",
              (await db.execute(select(Caller).where(Caller.phone_number == FROM)))
              .scalar_one_or_none() is not None)
        job_count = (await db.execute(
            select(func.count()).select_from(Job)
            .where(Job.type == "message_relay_ghl",
                   Job.payload["message_id"].astext == str(msg.id)))).scalar_one()
        check("relay job enqueued", job_count == 1)
        first_id = msg.id

    # idempotency: re-POST same MessageSid -> no duplicate Message row
    with httpx.Client(timeout=10) as c:
        p = sms_params(SID, FROM, TO_SW, "quote on the truck?")
        again = c.post(MSG_PATH, data=p, headers={**FWD, "X-SignalWire-Signature":
                       sign(settings.SIGNALWIRE_AUTH_TOKEN, MSG_URL, p)})
        check("re-POST accepted -> 200", again.status_code == 200)

    async with SessionLocal() as db:
        rows = (await db.execute(
            select(func.count()).select_from(Message)
            .where(Message.provider_message_sid == SID))).scalar_one()
        check("no duplicate message row (idempotent on sid)", rows == 1)
        same = (await db.execute(
            select(Message).where(Message.provider_message_sid == SID))).scalar_one()
        check("same row id preserved", same.id == first_id)

    await cleanup()  # also drops the relay jobs tied to our message
    print("\nALL MESSAGE CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit as e:
        print(e); sys.exit(1)
