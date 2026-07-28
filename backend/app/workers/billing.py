"""Asterisk CDR -> call_charges cost projection (Billing tab).

The DB-aware applier for the pure kernel in app/services/billing.py. Scans recent rows of
Asterisk's own `cdr` table — the ONLY per-leg record of what actually crossed the BulkVS
trunk — and writes one stamped `call_charges` row per billable leg.

Why not cost from `calls`: that table collapses every leg of a call onto one row (
provider_call_sid = Linkedid), so a flow-forwarded call — inbound leg + outbound leg, both
billed by BulkVS — would read as a single inbound charge and understate the bill by roughly
half. It is also missing durations on most rows; CDR's `billsec` is authoritative.

We do NOT own the `cdr` table (it is Asterisk's, via cdr_pgsql — see asterisk/README.md and
workers/asterisk_cdr.py). A missing table is caught and skipped exactly like a provider
outage, so this is safe to schedule before Asterisk is deployed.

IDEMPOTENCY / FROZEN HISTORY: the upsert is ON CONFLICT (uniqueid, kind) DO NOTHING. Once a
leg has been costed its row is never rewritten, so a later rate change cannot retroactively
alter what a past period cost — the same reasoning that stamps campaign_id at ingest
(ARCHITECTURE.md #1).
"""

import logging
import uuid as _uuid
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.db import SessionLocal
from app.models import BillingRate, Call, CallCharge, Number, Provider
from app.services import billing

logger = logging.getLogger("worker.billing")

PROVIDER_NAME = "asterisk"

# Same window/shape as workers/asterisk_cdr.py. `end` is a SQL keyword and must be quoted.
_CDR_QUERY = text(
    """
    SELECT linkedid, uniqueid, src, dst, channel, dstchannel, disposition,
           start, answer, "end", duration, billsec
    FROM cdr
    WHERE start >= now() - make_interval(hours => :hours)
    ORDER BY start, sequence
    """
)


def enabled() -> bool:
    return settings.ASTERISK_ENABLED


async def _load_rates(db) -> dict[str, BillingRate]:
    rows = (await db.execute(select(BillingRate))).scalars().all()
    return {r.code: r for r in rows}


async def _load_numbers(db) -> dict[str, Number]:
    """Our DIDs by E.164. Keyed on the media provider, matching how the flow runtime resolves
    a dialed DID (BulkVS DIDs are owned by 'bulkvs' but carry media on 'asterisk')."""
    rows = (
        await db.execute(
            select(Number).where(Number.media_provider == settings.BULKVS_MEDIA_PROVIDER)
        )
    ).scalars().all()
    return {n.phone_number: n for n in rows}


def _resolve_number(leg, numbers: dict[str, Number], entry_number: Number | None):
    """Which of OUR DIDs this leg belongs to.

    Inbound: the dialed number is `dst`. Outbound: the caller-ID we presented is `src` — but
    a flow-dial leg records neither (dst='s', src empty), so it inherits the DID from its
    call's entry leg, which is the number the caller originally dialed. That inheritance is
    what makes per-number cost correct for forwarded calls.
    """
    if leg.direction == "inbound":
        return numbers.get(leg.dst or "") or entry_number
    return numbers.get(leg.src or "") or entry_number


async def reconcile_charges(window_hours: int | None = None) -> int:
    """Cost every billable leg in the recent CDR window. Returns rows written. Idempotent."""
    hours = window_hours or settings.ASTERISK_CDR_WINDOW_HOURS

    try:
        async with SessionLocal() as db:
            rows = (await db.execute(_CDR_QUERY, {"hours": hours})).mappings().all()
    except Exception as exc:  # noqa: BLE001 - table absent / Asterisk not deployed -> skip
        logger.warning("billing: CDR scan failed (table absent?): %s", exc)
        return 0

    if not rows:
        return 0

    written = 0
    unrated = 0
    async with SessionLocal() as db:
        rates = await _load_rates(db)
        numbers = await _load_numbers(db)
        known_codes = set(rates)

        provider = (
            await db.execute(select(Provider).where(Provider.name == PROVIDER_NAME))
        ).scalar_one_or_none()

        # Our `calls` row for a linkedid, so a charge can be traced back to the call log.
        # Cached per run; one lookup per distinct call, not per leg.
        call_ids: dict[str, _uuid.UUID | None] = {}
        # The DID of each call's ENTRY leg, so secondary (forwarded) legs inherit it.
        entry_numbers: dict[str, Number | None] = {}

        for row in rows:
            leg = billing.classify_leg(dict(row), trunk_name=settings.BULKVS_TRUNK_NAME)
            if leg is None:
                continue  # operator WebRTC / Local legs never touch BulkVS

            if leg.direction == "inbound":
                entry_numbers.setdefault(leg.linkedid, numbers.get(leg.dst or ""))
            number = _resolve_number(leg, numbers, entry_numbers.get(leg.linkedid))

            if leg.linkedid not in call_ids:
                call_ids[leg.linkedid] = None
                if provider is not None:
                    call_ids[leg.linkedid] = (
                        await db.execute(
                            select(Call.id).where(
                                Call.provider_id == provider.id,
                                Call.provider_call_sid == leg.linkedid,
                            )
                        )
                    ).scalar_one_or_none()

            resolution = billing.resolve_rate_code(
                leg,
                number_tier=getattr(number, "tier", None),
                number_phone=getattr(number, "phone_number", None),
                known_codes=known_codes,
            )

            rate = rates.get(resolution.code) if resolution.code else None
            if rate is None:
                billed_seconds = 0
                amount = Decimal("0")
                increment = None
                rate_amount = None
                unrated += 1
            else:
                increment = rate.increment_seconds or billing.DEFAULT_INCREMENT_SECONDS
                billed_seconds = billing.round_billsec(
                    leg.raw_billsec, increment, rate.minimum_seconds or 0
                )
                rate_amount = rate.amount
                amount = billing.cost_of_minutes(billed_seconds, rate.amount)

            result = await db.execute(
                pg_insert(CallCharge)
                .values(
                    id=_uuid.uuid4(),
                    uniqueid=leg.uniqueid,
                    linkedid=leg.linkedid,
                    kind=billing.KIND_MINUTES,
                    call_id=call_ids.get(leg.linkedid),
                    number_id=number.id if number is not None else None,
                    direction=leg.direction,
                    channel=leg.channel,
                    src=leg.src,
                    dst=leg.dst,
                    dest_unknown=leg.dest_unknown,
                    started_at=row.get("start"),
                    raw_billsec=leg.raw_billsec,
                    billed_seconds=billed_seconds,
                    rate_code=resolution.code,
                    rate_amount=rate_amount,
                    increment_seconds=increment,
                    amount=amount,
                    unrated=resolution.unrated,
                    unrated_reason=resolution.unrated_reason,
                )
                # Frozen once costed: never re-price an existing leg.
                .on_conflict_do_nothing(index_elements=["uniqueid", "kind"])
            )
            written += result.rowcount or 0

            # A CNAM dip is billed per INBOUND call on a DID with carrier-side CNAM delivery
            # enabled. It is per-event, not per-minute, so at short call durations it can
            # exceed the minutes charge — worth itemizing rather than folding in.
            cnam_rate = rates.get(billing.CNAM_DIP)
            if (
                leg.direction == "inbound"
                and number is not None
                and getattr(number, "cnam_enabled", False)
                and cnam_rate is not None
            ):
                res = await db.execute(
                    pg_insert(CallCharge)
                    .values(
                        id=_uuid.uuid4(),
                        uniqueid=leg.uniqueid,
                        linkedid=leg.linkedid,
                        kind=billing.KIND_CNAM,
                        call_id=call_ids.get(leg.linkedid),
                        number_id=number.id,
                        direction=leg.direction,
                        channel=leg.channel,
                        src=leg.src,
                        dst=leg.dst,
                        started_at=row.get("start"),
                        raw_billsec=0,
                        billed_seconds=0,
                        rate_code=billing.CNAM_DIP,
                        rate_amount=cnam_rate.amount,
                        amount=billing.quantize_money(cnam_rate.amount),
                    )
                    .on_conflict_do_nothing(index_elements=["uniqueid", "kind"])
                )
                written += res.rowcount or 0

        await db.commit()

    logger.info(
        "billing: scanned %s CDR rows, wrote %s charge rows (%s unrated) over last %sh",
        len(rows), written, unrated, hours,
    )
    return written
