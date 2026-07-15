"""Phase 3 (SignalWire adapter) + Phase 4 (read API) end-to-end.

- SignalWire webhook verified with the SignalWire token (separate from Twilio).
- Seeds a campaign+number, ingests calls from BOTH providers, then exercises the
  read API: /api/calls (list+filter), /api/calls/{id}, /api/numbers, /api/callers,
  /api/dashboard/summary. Cleans up after itself.

Run against a server on :8899.
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
FWD = {"x-forwarded-proto": "https", "x-forwarded-host": "api.example.com"}

SW_URL = "https://api.example.com/webhooks/signalwire/status"
SW_PATH = f"{BASE}/webhooks/signalwire/status"
TW_URL = "https://api.example.com/webhooks/twilio/status"
TW_PATH = f"{BASE}/webhooks/twilio/status"

TO_TW = "+13055540001"   # tracking number on Twilio
TO_SW = "+13055540002"   # tracking number on SignalWire
SIDS = ["CA_p34_tw", "CA_p34_sw"]
FROMS = ["+13055541111", "+13055542222"]


def sign(secret: str, url: str, params: dict) -> str:
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(secret.encode(), data.encode(), hashlib.sha1).digest()).decode()


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"phase34 failed at: {name}")


async def cleanup():
    async with SessionLocal() as db:
        for sid in SIDS:
            call = (await db.execute(select(Call).where(Call.provider_call_sid == sid))).scalar_one_or_none()
            if call:
                await db.execute(delete(CallEvent).where(CallEvent.call_id == call.id))
                await db.execute(delete(Call).where(Call.id == call.id))
        for f in FROMS:
            await db.execute(delete(Caller).where(Caller.phone_number == f))
        await db.execute(delete(Number).where(Number.phone_number.in_([TO_TW, TO_SW])))
        await db.execute(text("DELETE FROM campaigns WHERE name='P34 Campaign'"))
        await db.commit()


async def _provider(db, name):
    p = (await db.execute(select(Provider).where(Provider.name == name))).scalar_one_or_none()
    if not p:
        p = Provider(name=name); db.add(p); await db.flush()
    return p


async def seed():
    async with SessionLocal() as db:
        tw = await _provider(db, "twilio")
        sw = await _provider(db, "signalwire")
        camp = Campaign(name="P34 Campaign", source="facebook")
        db.add(camp); await db.flush()
        db.add(Number(provider_id=tw.id, campaign_id=camp.id, phone_number=TO_TW,
                      friendly_name="P34 Twilio", forwards_to="+13055550000", active=True))
        db.add(Number(provider_id=sw.id, campaign_id=camp.id, phone_number=TO_SW,
                      friendly_name="P34 SignalWire", forwards_to="+13055550000", active=True))
        await db.commit()


def status_params(sid, frm, status, to):
    return {"CallSid": sid, "From": frm, "To": to, "Direction": "inbound",
            "CallStatus": status, "CallDuration": "55"}


async def main():
    await cleanup(); await seed()

    with httpx.Client(timeout=10) as c:
        print("phase 3 — signalwire signature (separate token):")
        p = status_params(SIDS[1], FROMS[1], "completed", TO_SW)
        bad = c.post(SW_PATH, data=p, headers={**FWD, "X-SignalWire-Signature":
                     sign(settings.TWILIO_AUTH_TOKEN, SW_URL, p)})  # wrong key on purpose
        check("signalwire rejects twilio-key signature -> 403", bad.status_code == 403)
        good = c.post(SW_PATH, data=p, headers={**FWD, "X-SignalWire-Signature":
                      sign(settings.SIGNALWIRE_AUTH_TOKEN, SW_URL, p)})
        check("signalwire accepts signalwire-key signature -> 200", good.status_code == 200)

        # a twilio call too
        pt = status_params(SIDS[0], FROMS[0], "completed", TO_TW)
        check("twilio call ingested -> 200",
              c.post(TW_PATH, data=pt, headers={**FWD, "X-Twilio-Signature":
                     sign(settings.TWILIO_AUTH_TOKEN, TW_URL, pt)}).status_code == 200)

        print("auth + phase 4 read API:")
        token = c.post(f"{BASE}/api/auth/login",
                       data={"username": "admin@example.com", "password": "admin-dev-pass"}).json()["access_token"]
        H = {"Authorization": f"Bearer {token}"}

        check("GET /api/calls requires auth -> 401", c.get(f"{BASE}/api/calls").status_code == 401)

        calls = c.get(f"{BASE}/api/calls", headers=H).json()
        check("calls list has >=2 items", calls["total"] >= 2 and len(calls["items"]) >= 2)
        item = next(i for i in calls["items"] if i["provider_call_sid"] == SIDS[1])
        check("signalwire call shows provider=signalwire", item["provider"] == "signalwire")
        check("call carries campaign attribution", item["campaign_name"] == "P34 Campaign")

        # filter by provider
        sw_only = c.get(f"{BASE}/api/calls", params={"provider": "signalwire"}, headers=H).json()
        check("provider filter works", all(i["provider"] == "signalwire" for i in sw_only["items"]))

        # detail
        detail = c.get(f"{BASE}/api/calls/{item['id']}", headers=H).json()
        check("call detail has events timeline", len(detail["events"]) >= 1)

        numbers = c.get(f"{BASE}/api/numbers", headers=H).json()
        n_tw = next(x for x in numbers if x["phone_number"] == TO_TW)
        n_sw = next(x for x in numbers if x["phone_number"] == TO_SW)
        check("numbers endpoint reports per-number volume + provider",
              n_tw["total_calls"] >= 1 and n_tw["provider"] == "twilio"
              and n_sw["total_calls"] >= 1 and n_sw["provider"] == "signalwire")

        callers = c.get(f"{BASE}/api/callers", headers=H).json()
        check("callers endpoint returns rows", callers["total"] >= 2)

        summary = c.get(f"{BASE}/api/dashboard/summary", params={"range": "7d"}, headers=H).json()
        check("dashboard totals", summary["total_calls"] >= 2)
        check("dashboard new-for-campaign counts", summary["new_for_campaign"] >= 2)
        check("dashboard by_campaign present",
              any(b["campaign"] == "P34 Campaign" for b in summary["by_campaign"]))
        check("dashboard daily series present", len(summary["daily"]) >= 1)
        check("dashboard top_callers present", len(summary["top_callers"]) >= 2)

    await cleanup()
    print("\nALL PHASE 3+4 CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit as e:
        print(e); sys.exit(1)
