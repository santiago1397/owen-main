"""The live-call registry for Retell calls, backed by the `retell_calls` table.

Why a table (see `models.RetellCall` for the long form): the worker runs the call and the app
answers Retell, and they share nothing in memory. Everything one of them must tell the other
goes through here:

    worker -> app   the call exists, and on which channels / bridge (functions, takeover)
    app -> worker   a function asked to leave: `transfer` or `end_call` (the wait loop polls)
    app -> worker   Retell's `call_ended` arrived (ended_event_at): if the leg still looks up
                    here its BYE was lost, and the wait loop ends it (`retell_said_ended`)
    app -> app      Retell retried a webhook (claimed once, by a conditional UPDATE)

The interface is small and every method answers with plain dicts, so the engine, the routes
and the webhook can be exercised against `tests/retell_support.MemoryRegistry` with no
database. `use()` swaps the implementation; `current()` is what the code calls.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("integrations.retell.registry")

LIVE = "live"
ENDED = "ended"

# The columns a snapshot carries. Channel and bridge ids are in it because takeover needs
# them server-side; no route ever returns a snapshot to the CRM as it is.
FIELDS = (
    "retell_call_id", "linkedid", "status", "retell_agent_id", "agent_name",
    "agent_version_id", "caller_number", "dialed_number", "call_channel_id",
    "retell_channel_id", "bridge_id", "exit_port", "exit_data", "captured", "requests",
    "retell_agent_version", "cost_cents", "disconnection_reason", "summary", "sentiment",
    "successful", "ended_event_at", "analyzed_event_at", "created_at", "ended_at",
)

EVENT_COLUMNS = {"ended": "ended_event_at", "analyzed": "analyzed_event_at"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def snapshot(row) -> dict:
    out = {f: getattr(row, f, None) for f in FIELDS}
    if out.get("agent_version_id") is not None:
        out["agent_version_id"] = str(out["agent_version_id"])
    if out.get("cost_cents") is not None:
        out["cost_cents"] = float(out["cost_cents"])
    return out


def retell_said_ended(row: dict | None) -> bool:
    """Has Retell's `call_ended` webhook been received for this row? PURE.

    `ended_event_at` is the claim; `disconnection_reason` is stored by the same claim and is
    NOT cleared when a failed claim is released for Retell's retry — so the engine's safety
    net (agents/retell.py `_wait`) still sees the end while the retry is pending. `call_analyzed`
    writes neither."""
    if not row:
        return False
    return row.get("ended_event_at") is not None or bool(row.get("disconnection_reason"))


class DbRegistry:
    """`retell_calls`, one short session per operation — a call can last many minutes and
    must never hold a transaction open (the runtime's session-per-write rule)."""

    async def _row(self, db, retell_call_id: str):
        from sqlalchemy import select

        from app.models import RetellCall

        return (await db.execute(
            select(RetellCall).where(RetellCall.retell_call_id == str(retell_call_id))
        )).scalar_one_or_none()

    async def create(self, **fields) -> None:
        import uuid as _uuid

        from app.db import SessionLocal
        from app.models import RetellCall

        values = {k: v for k, v in fields.items() if k in FIELDS}
        if values.get("agent_version_id"):
            values["agent_version_id"] = _uuid.UUID(str(values["agent_version_id"]))
        values.setdefault("status", LIVE)
        async with SessionLocal() as db:
            db.add(RetellCall(**values))
            await db.commit()

    async def update(self, retell_call_id: str, **fields) -> None:
        from app.db import SessionLocal

        async with SessionLocal() as db:
            row = await self._row(db, retell_call_id)
            if row is None:
                return
            for k, v in fields.items():
                if k in FIELDS:
                    setattr(row, k, v)
            await db.commit()

    async def get(self, retell_call_id: str) -> Optional[dict]:
        from app.db import SessionLocal

        if not retell_call_id:
            return None
        async with SessionLocal() as db:
            row = await self._row(db, retell_call_id)
            return snapshot(row) if row is not None else None

    async def live(self) -> list[dict]:
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import RetellCall

        async with SessionLocal() as db:
            rows = (await db.execute(
                select(RetellCall).where(RetellCall.status == LIVE)
                .order_by(RetellCall.created_at)
            )).scalars().all()
            return [snapshot(r) for r in rows]

    async def live_for(self, linkedid: str) -> Optional[dict]:
        for row in await self.live():
            if row.get("linkedid") == linkedid:
                return row
        return None

    async def exit_request(self, retell_call_id: str) -> Optional[tuple[str, dict]]:
        row = await self.get(retell_call_id)
        if row is None or not row.get("exit_port"):
            return None
        return str(row["exit_port"]), dict(row.get("exit_data") or {})

    async def transfer_tried(self, linkedid: str, *, exclude_call_id: str = "") -> bool:
        """Did another Retell session on this call (same linkedid) ask to transfer? True only
        for the agent taking the caller back after an unanswered transfer (2026-10-08)."""
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import RetellCall

        async with SessionLocal() as db:
            found = (await db.execute(
                select(RetellCall.id).where(
                    RetellCall.linkedid == str(linkedid),
                    RetellCall.retell_call_id != str(exclude_call_id),
                    RetellCall.exit_port == "transfer",
                ).limit(1)
            )).scalar_one_or_none()
            return found is not None

    async def request_exit(self, retell_call_id: str, port: str, data: dict) -> bool:
        """Ask the worker to end the agent's part. First request wins: a retried function
        call (Retell retries) or a second tool call cannot change where the caller goes."""
        from sqlalchemy import update

        from app.db import SessionLocal
        from app.models import RetellCall

        async with SessionLocal() as db:
            result = await db.execute(
                update(RetellCall)
                .where(RetellCall.retell_call_id == str(retell_call_id),
                       RetellCall.status == LIVE, RetellCall.exit_port.is_(None))
                .values(exit_port=str(port), exit_data=dict(data or {}))
            )
            await db.commit()
            return bool(result.rowcount)

    async def merge_capture(self, retell_call_id: str, fields: dict) -> bool:
        from app.db import SessionLocal

        async with SessionLocal() as db:
            row = await self._row(db, retell_call_id)
            if row is None:
                return False
            merged = dict(row.captured or {})
            merged.update({k: v for k, v in (fields or {}).items() if v not in (None, "")})
            row.captured = merged
            await db.commit()
            return True

    async def add_request(self, retell_call_id: str, request: dict) -> None:
        from app.db import SessionLocal

        async with SessionLocal() as db:
            row = await self._row(db, retell_call_id)
            if row is None:
                return
            existing = list(row.requests or [])
            if request not in existing:
                row.requests = existing + [dict(request)]
            await db.commit()

    async def finish(self, retell_call_id: str) -> Optional[dict]:
        from app.db import SessionLocal

        async with SessionLocal() as db:
            row = await self._row(db, retell_call_id)
            if row is None:
                return None
            if row.status != ENDED:
                row.status = ENDED
                row.ended_at = _now()
                await db.commit()
            return snapshot(row)

    async def claim_event(self, retell_call_id: str, kind: str, fields: dict) -> Optional[dict]:
        """Mark webhook `kind` ("ended" | "analyzed") processed and store its fields — ONCE.

        A conditional UPDATE (`... WHERE <kind>_event_at IS NULL`), so two deliveries of the
        same event racing each other still claim it exactly once. None = already claimed (or
        unknown): the caller writes nothing."""
        from sqlalchemy import update

        from app.db import SessionLocal
        from app.models import RetellCall

        column = EVENT_COLUMNS[kind]
        values = {k: v for k, v in (fields or {}).items() if k in FIELDS}
        values[column] = _now()
        async with SessionLocal() as db:
            result = await db.execute(
                update(RetellCall)
                .where(RetellCall.retell_call_id == str(retell_call_id),
                       getattr(RetellCall, column).is_(None))
                .values(**values)
            )
            await db.commit()
            if not result.rowcount:
                return None
            row = await self._row(db, retell_call_id)
            return snapshot(row) if row is not None else None

    async def release_event(self, retell_call_id: str, kind: str) -> None:
        """Undo a claim whose processing failed, so Retell's retry can do it."""
        await self.update(retell_call_id, **{EVENT_COLUMNS[kind]: None})


_current = DbRegistry()


def current():
    return _current


def use(registry) -> object:
    """Swap the registry (tests). Returns the previous one so it can be restored."""
    global _current
    previous, _current = _current, registry
    return previous
