"""Billing tab API — BulkVS cost ESTIMATE from our own call data.

Reads the `call_charges` projection (one stamped row per billable leg, written by
workers/billing.py) plus the recurring side derived from the `numbers` inventory. Never
claims to be an invoice: BulkVS publishes no usage/billing API, so every figure here is
computed by us and labelled as an estimate in the UI.

Date handling follows the dashboard idiom exactly: the frontend sends explicit half-open UTC
bounds and day buckets are cut in the business timezone (ARCHITECTURE.md #10).
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.config import settings
from app.db import get_db
from app.models import BillingAdjustment, BillingRate, CallCharge, Number, User
from app.services import billing
from app.services.number_sync import is_carrier_active

router = APIRouter(prefix="/api/billing", tags=["billing"])


def _f(value) -> float:
    """Decimal -> float for JSON. Money is stored at 6dp; the UI renders 4dp."""
    return float(value or 0)


def _window(date_from: datetime | None, date_to: datetime | None) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return (date_from or (now - timedelta(days=7)), date_to or now)


# --- summary --------------------------------------------------------------------------------


@router.get("/summary")
async def summary(
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    start, end = _window(date_from, date_to)
    in_range = and_(CallCharge.started_at >= start, CallCharge.started_at < end)

    # --- usage, broken out by the rate that priced it -------------------------------------
    # Grouping by rate_code (not just direction) is what makes every figure explainable: a
    # blended per-direction number can't show WHY it is what it is.
    rows = (
        await db.execute(
            select(
                CallCharge.kind,
                CallCharge.direction,
                CallCharge.rate_code,
                CallCharge.rate_amount,
                func.count().label("legs"),
                func.sum(CallCharge.billed_seconds).label("billed_seconds"),
                func.sum(CallCharge.amount).label("amount"),
            )
            .where(in_range, CallCharge.unrated.is_(False))
            .group_by(
                CallCharge.kind, CallCharge.direction,
                CallCharge.rate_code, CallCharge.rate_amount,
            )
            .order_by(func.sum(CallCharge.amount).desc())
        )
    ).mappings().all()

    labels = {
        r.code: r.label for r in (await db.execute(select(BillingRate))).scalars().all()
    }
    usage_lines = [
        {
            "kind": r["kind"],
            "direction": r["direction"],
            "rate_code": r["rate_code"],
            "rate_label": labels.get(r["rate_code"], r["rate_code"]),
            "rate_amount": _f(r["rate_amount"]),
            "legs": r["legs"],
            "billed_seconds": int(r["billed_seconds"] or 0),
            "amount": _f(r["amount"]),
        }
        for r in rows
    ]
    usage_total = sum(Decimal(str(line["amount"])) for line in usage_lines) or Decimal("0")

    # --- honesty counters ------------------------------------------------------------------
    # Legs we could not price are reported, never folded into the total as $0.
    unrated_rows = (
        await db.execute(
            select(
                CallCharge.unrated_reason,
                func.count().label("legs"),
                func.sum(CallCharge.raw_billsec).label("raw_billsec"),
            )
            .where(in_range, CallCharge.unrated.is_(True))
            .group_by(CallCharge.unrated_reason)
            .order_by(func.count().desc())
        )
    ).mappings().all()
    unrated = [
        {"reason": r["unrated_reason"], "legs": r["legs"],
         "raw_billsec": int(r["raw_billsec"] or 0)}
        for r in unrated_rows
    ]
    # Forwarded legs whose destination Asterisk never recorded (dst='s'). Priced as domestic
    # because this trunk only serves NANP, but surfaced so the assumption is visible.
    dest_unknown_legs = (
        await db.execute(
            select(func.count()).select_from(CallCharge)
            .where(in_range, CallCharge.dest_unknown.is_(True))
        )
    ).scalar_one()

    # --- per-day, bucketed in the business timezone ----------------------------------------
    local_ts = func.timezone(settings.BUSINESS_TZ, CallCharge.started_at)
    day = func.date_trunc("day", local_ts)
    per_day = [
        {
            "day": d.date().isoformat() if d else None,
            "legs": legs,
            "billed_seconds": int(secs or 0),
            "amount": _f(amt),
        }
        for d, legs, secs, amt in (
            await db.execute(
                select(
                    day.label("day"), func.count(),
                    func.sum(CallCharge.billed_seconds), func.sum(CallCharge.amount),
                )
                .where(in_range).group_by("day").order_by("day")
            )
        ).all()
    ]

    # --- recurring (the dominant cost at low call volume) ------------------------------------
    recurring = await _recurring(db, start, end)

    # --- manual adjustments falling in the window --------------------------------------------
    adj_rows = (
        await db.execute(
            select(BillingAdjustment)
            .where(
                BillingAdjustment.occurred_on >= start.date(),
                BillingAdjustment.occurred_on < end.date() + timedelta(days=1),
            )
            .order_by(BillingAdjustment.occurred_on.desc())
        )
    ).scalars().all()
    adjustments = [
        {
            "id": str(a.id), "occurred_on": a.occurred_on.isoformat(), "code": a.code,
            "description": a.description, "amount": _f(a.amount),
        }
        for a in adj_rows
    ]
    adjustments_total = sum((Decimal(str(a["amount"])) for a in adjustments), Decimal("0"))

    monthly_total = Decimal(str(recurring["monthly_total"]))
    onetime_total = Decimal(str(recurring["onetime_total"]))
    return {
        "range_from": start,
        "range_to": end,
        "usage_lines": usage_lines,
        "usage_total": _f(usage_total),
        "unrated": unrated,
        "unrated_legs": sum(u["legs"] for u in unrated),
        "dest_unknown_legs": dest_unknown_legs,
        "per_day": per_day,
        "recurring": recurring,
        "adjustments": adjustments,
        "adjustments_total": _f(adjustments_total),
        "grand_total": _f(usage_total + monthly_total + onetime_total + adjustments_total),
        # Surfaced so the UI can caveat the headline until a real invoice has confirmed the
        # billing increment (the one rate not read off the operator's own price sheet).
        "increment_seconds": billing.DEFAULT_INCREMENT_SECONDS,
    }


async def _recurring(db: AsyncSession, start: datetime, end: datetime) -> dict:
    """Monthly per-DID charges + any one-time setup fees whose activation falls in-window.

    Deliberately NOT prorated: BulkVS's proration behaviour is unconfirmed, and on a $0.06
    monthly the entire proration error is a fraction of a cent. Counting whole months errs
    very slightly expensive, which is the right direction for a no-surprises monitor.
    """
    rates = {r.code: r for r in (await db.execute(select(BillingRate))).scalars().all()}
    numbers = (
        await db.execute(
            select(Number).where(
                Number.owner_provider == settings.BULKVS_OWNER_PROVIDER,
                Number.active.is_(True),
            ).order_by(Number.phone_number)
        )
    ).scalars().all()

    e911_rate = rates.get(billing.E911_MONTHLY)
    setup_rate = rates.get(billing.DID_SETUP)
    lines: list[dict] = []
    monthly_total = Decimal("0")
    onetime_total = Decimal("0")

    for n in numbers:
        # A DID still provisioning at the carrier (e.g. a SUBMITTED port-in) isn't billed yet.
        if not is_carrier_active(n.provider_status):
            continue
        if billing.is_toll_free(n.phone_number):
            code = billing.TOLLFREE_MONTHLY
        else:
            code = f"{billing.DID_MONTHLY_PREFIX}{(n.tier or '').strip()}" if n.tier else None
        rate = rates.get(code) if code else None
        monthly = Decimal(str(rate.amount)) if rate is not None else Decimal("0")
        e911 = (
            Decimal(str(e911_rate.amount))
            if (n.e911_enabled and e911_rate is not None) else Decimal("0")
        )
        monthly_total += monthly + e911

        setup = Decimal("0")
        if (
            setup_rate is not None
            and n.activation_date is not None
            and start <= n.activation_date < end
        ):
            setup = Decimal(str(setup_rate.amount))
            onetime_total += setup

        lines.append({
            "number_id": str(n.id),
            "phone_number": n.phone_number,
            "friendly_name": n.friendly_name,
            "tier": n.tier,
            "monthly": _f(monthly),
            "e911": _f(e911),
            "setup_this_period": _f(setup),
            # No tier means no monthly rate could be resolved — same honesty rule as usage.
            "unrated": rate is None,
        })

    return {
        "numbers": lines,
        "monthly_total": _f(monthly_total),
        "onetime_total": _f(onetime_total),
    }


# --- leg log ----------------------------------------------------------------------------------


@router.get("/legs")
async def legs(
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    direction: str | None = None,
    number_id: uuid.UUID | None = None,
    unrated_only: bool = False,
    limit: int = 200,
    offset: int = 0,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The per-leg charge log — the unit BulkVS actually meters. `linkedid` groups the legs
    belonging to one caller-facing call, so a forwarded call shows as its two real charges."""
    start, end = _window(date_from, date_to)
    filters = [CallCharge.started_at >= start, CallCharge.started_at < end]
    if direction in ("inbound", "outbound"):
        filters.append(CallCharge.direction == direction)
    if number_id is not None:
        filters.append(CallCharge.number_id == number_id)
    if unrated_only:
        filters.append(CallCharge.unrated.is_(True))

    total = (
        await db.execute(select(func.count()).select_from(CallCharge).where(*filters))
    ).scalar_one()

    rows = (
        await db.execute(
            select(CallCharge, Number.phone_number, Number.friendly_name)
            .join(Number, CallCharge.number_id == Number.id, isouter=True)
            .where(*filters)
            .order_by(CallCharge.started_at.desc(), CallCharge.uniqueid)
            .limit(min(max(limit, 1), 1000)).offset(max(offset, 0))
        )
    ).all()

    return {
        "total": total,
        "items": [
            {
                "id": str(c.id),
                "linkedid": c.linkedid,
                "kind": c.kind,
                "call_id": str(c.call_id) if c.call_id else None,
                "direction": c.direction,
                "our_number": phone,
                "our_number_label": friendly,
                "other_party": c.src if c.direction == "inbound" else c.dst,
                "dest_unknown": c.dest_unknown,
                "at": c.started_at,
                "raw_billsec": c.raw_billsec,
                "billed_seconds": c.billed_seconds,
                "rate_code": c.rate_code,
                "rate_amount": _f(c.rate_amount) if c.rate_amount is not None else None,
                "amount": _f(c.amount),
                "unrated": c.unrated,
                "unrated_reason": c.unrated_reason,
            }
            for c, phone, friendly in rows
        ],
    }


# --- rate table (transparency) ------------------------------------------------------------------


@router.get("/rates")
async def list_rates(
    _: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> list[dict]:
    """The price sheet as configured. `source` shows provenance — 'sheet' was read off the
    operator's own BulkVS portal; 'web' filled a gap the portal only linked out to — so a
    surprising total can always be traced back to a rate nobody verified."""
    rows = (
        await db.execute(select(BillingRate).order_by(BillingRate.unit, BillingRate.code))
    ).scalars().all()
    return [
        {
            "code": r.code, "label": r.label, "unit": r.unit, "amount": _f(r.amount),
            "increment_seconds": r.increment_seconds, "minimum_seconds": r.minimum_seconds,
            "lnp_fee": _f(r.lnp_fee) if r.lnp_fee is not None else None,
            "source": r.source,
        }
        for r in rows
    ]


# --- manual adjustments -------------------------------------------------------------------------


class AdjustmentIn(BaseModel):
    occurred_on: date
    code: str
    amount: float
    description: str | None = None
    number_id: uuid.UUID | None = None


@router.post("/adjustments")
async def add_adjustment(
    payload: AdjustmentIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Record an account-level charge that has no call data behind it — an LNP port fee, E911
    overage, a LIDB update, a directory listing. These are the price-sheet items that simply
    cannot be derived from CDR, so they are entered by hand rather than invented."""
    code = (payload.code or "").strip()
    if not code:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "code is required")
    row = BillingAdjustment(
        occurred_on=payload.occurred_on,
        code=code,
        amount=Decimal(str(payload.amount)),
        description=(payload.description or "").strip() or None,
        number_id=payload.number_id,
        created_by_user_id=user.id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"id": str(row.id), "occurred_on": row.occurred_on.isoformat(),
            "code": row.code, "amount": _f(row.amount)}


@router.delete("/adjustments/{adjustment_id}")
async def delete_adjustment(
    adjustment_id: uuid.UUID,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(BillingAdjustment, adjustment_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "adjustment not found")
    await db.delete(row)
    await db.commit()
    return {"ok": True}
