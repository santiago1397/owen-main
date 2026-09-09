"""Request every JWT-authed GET against the REAL app and the REAL database.

WHY THIS EXISTS. `/api/calls/{id}` returned 500 for five days because a capture query
referenced `call.id` in a function whose variable is `call_id`. Nothing caught it: the unit
tests never build the app, the smoke tests only covered `/api/ai/*` plus login and the
dashboard, and the UI renders a failed fetch as an empty drawer. One authenticated GET would
have found it on the day it shipped.

So this is deliberately shallow and wide: it asserts only that every read endpoint the UI
depends on answers without a server error, including one DETAIL route per collection —
because detail routes are where the joins live, and joins are where this class of bug hides.

Runs INSIDE the app container against production data, read-only:

    docker compose exec -T app python -m tests.smoke_routes     # or: make smoke-routes

Exits non-zero on the first 5xx.
"""

import asyncio
import sys

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.security import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import Call, InboundEmail, Number, User

# GETs with no path parameters. A 200 is expected; anything 5xx is a failure. 503 is allowed
# because the telephony surface answers that honestly when ASTERISK_ENABLED is off.
COLLECTIONS = [
    "/api/auth/me",
    "/api/calls?limit=5",
    "/api/callers?limit=5",
    "/api/numbers",
    "/api/campaigns",
    "/api/dashboard/summary?range=last_7d",
    "/api/emails?limit=5",
    "/api/messages?limit=5",
    "/api/inbox/threads?limit=5",
    "/api/flows",
    "/api/agents",
    "/api/billing/summary",
    "/api/settings",
    "/api/api-keys",
    "/health",
]

OK_NON_200 = {401, 403, 404, 422, 503}


async def _detail_targets(db) -> list[str]:
    """One real id per collection, so the detail routes are exercised against real joins."""
    out = []
    call_id = (await db.execute(
        select(Call.id).where(Call.started_at.is_not(None))
        .order_by(Call.started_at.desc()).limit(1)
    )).scalars().first()
    if call_id:
        out.append(f"/api/calls/{call_id}")
    number_id = (await db.execute(select(Number.id).limit(1))).scalars().first()
    if number_id:
        out.append(f"/api/numbers/{number_id}")
    email_id = (await db.execute(select(InboundEmail.id).limit(1))).scalars().first()
    if email_id:
        out.append(f"/api/emails/{email_id}")
    return out


async def main() -> int:
    async with SessionLocal() as db:
        user = (await db.execute(
            select(User).where(User.active.is_(True)).limit(1)
        )).scalars().first()
        if user is None:
            print("no active user to authenticate as — create one first")
            return 2
        targets = COLLECTIONS + await _detail_targets(db)

    token = create_access_token(user.email)
    headers = {"Authorization": f"Bearer {token}"}
    failures = []

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://smoke") as c:
        for path in targets:
            try:
                r = await c.get(path, headers=headers)
                code = r.status_code
            except Exception as exc:  # noqa: BLE001 - an exception IS the failure
                print(f"  RAISED  {path}: {exc!r}")
                failures.append(path)
                continue
            if code >= 500:
                print(f"  {code}     {path}")
                print(f"          {r.text[:200]}")
                failures.append(path)
            elif code == 200 or code in OK_NON_200:
                print(f"  {code}     {path}")
            else:
                print(f"  {code}?    {path}")

    print()
    if failures:
        print(f"SMOKE FAILED — {len(failures)} endpoint(s) returned a server error:")
        for f in failures:
            print(f"  {f}")
        return 1
    print(f"All {len(targets)} routes answered without a server error.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
