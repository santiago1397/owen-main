"""BulkVS /voice -> call_charges cost projection (Billing tab).

Costs come from BULKVS'S OWN RATED RECORDS (`GET /voice`), which carry `perMinute` and
`amount` per leg. Nothing here re-derives a price: the number shown is the number they
charged.

WHY NOT ASTERISK CDR (the original design): checking Asterisk's CDR against real BulkVS
records showed it wrong in both directions —
  - Asterisk reported billsec=0 on flow-dial legs that BulkVS billed for 7s and 21s, so
    forwarded calls were priced at $0.00 (failing CHEAP, the dangerous direction);
  - outbound is not a flat $0.004: an observed call rated at $0.0099/min, which a single
    "outbound domestic" rate cannot express.
Asterisk CDR is still used, read-only, for CORRELATION — matching a BulkVS record back to
the Linkedid (and therefore the `calls` row) it belongs to, so a charge can be traced to
the call log. Correlation failing only costs context, never money.

IDEMPOTENCY: upsert ON CONFLICT (uniqueid, kind) where uniqueid is the BulkVS callID. Unlike
the estimate era there is nothing to "freeze" — BulkVS's amount IS the truth — so an existing
row is UPDATED if they restate it (rare, but their record wins over ours by definition).
"""

import logging
import uuid as _uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.models import Call, CallCharge, Number, Provider
from app.db import SessionLocal
from app.providers import bulkvs_client
from app.services import billing

logger = logging.getLogger("worker.billing")

PROVIDER_NAME = "asterisk"

# Correlate a BulkVS record to an Asterisk leg by matching the call's own numbers within a
# tolerance around its start. BulkVS timestamps come from their switch and Asterisk's from
# ours, so they differ by a second or two on the same call.
_CORRELATION_WINDOW_SECONDS = 120

_CORRELATE_QUERY = text(
    """
    SELECT linkedid
    FROM cdr
    WHERE start BETWEEN :lo AND :hi
      AND (
            regexp_replace(coalesce(src, ''), '\\D', '', 'g') = :a
         OR regexp_replace(coalesce(dst, ''), '\\D', '', 'g') = :a
      )
    ORDER BY abs(extract(epoch FROM (start - :at)))
    LIMIT 1
    """
)


def enabled() -> bool:
    """Gated on BulkVS REST creds — this feed is the carrier's, not Asterisk's, so it works
    even if the local telephony stack is down."""
    return bool(settings.BULKVS_API_USERNAME and settings.BULKVS_API_PASSWORD)


def _digits(value) -> str:
    return "".join(c for c in str(value or "") if c.isdigit())


async def _load_numbers(db) -> dict[str, Number]:
    """Our DIDs keyed by BARE DIGITS. BulkVS is inconsistent about the leading '+' between
    records (`16452516222` and `+16452516222` both appear for the same DID), so matching on
    the E.164 string directly would miss half of them."""
    rows = (
        await db.execute(
            select(Number).where(Number.owner_provider == settings.BULKVS_OWNER_PROVIDER)
        )
    ).scalars().all()
    return {_digits(n.phone_number): n for n in rows}


async def _correlate(db, charge, provider_id) -> tuple[str | None, _uuid.UUID | None]:
    """Best-effort (linkedid, call_id) for a BulkVS record, via the local Asterisk CDR.

    Never raises: the `cdr` table belongs to Asterisk and may be absent entirely. Losing
    correlation costs only the link to the call log, never the charge.
    """
    if charge.started_at is None:
        return None, None
    other = _digits(charge.source if charge.direction == billing.VOICE_INBOUND
                    else charge.destination)
    if not other:
        return None, None
    try:
        lo = charge.started_at - timedelta(seconds=_CORRELATION_WINDOW_SECONDS)
        hi = charge.started_at + timedelta(seconds=_CORRELATION_WINDOW_SECONDS)
        linkedid = (
            await db.execute(
                _CORRELATE_QUERY, {"lo": lo, "hi": hi, "at": charge.started_at, "a": other}
            )
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - cdr absent / unreadable -> just no correlation
        return None, None
    if not linkedid or provider_id is None:
        return linkedid, None
    call_id = (
        await db.execute(
            select(Call.id).where(
                Call.provider_id == provider_id, Call.provider_call_sid == linkedid
            )
        )
    ).scalar_one_or_none()
    return linkedid, call_id


def _number_for(charge, numbers: dict[str, Number]) -> Number | None:
    """Which of OUR DIDs a record belongs to. For inbound that's the destination (the DID
    dialed); for outbound it's the source (the caller-ID we presented). A forwarded leg
    presents the ORIGINAL caller's number as source, so it may match neither — the charge is
    still recorded, just without per-number attribution."""
    if charge.direction == billing.VOICE_INBOUND:
        return numbers.get(_digits(charge.destination))
    return numbers.get(_digits(charge.source)) or numbers.get(_digits(charge.destination))


async def purge_estimated_charges() -> int:
    """One-shot cleanup of rows written by the superseded Asterisk-CDR estimate.

    Removes the bogus per-call CNAM charges (BulkVS does not bill them) and any minutes rows
    keyed by an Asterisk uniqueid, which the /voice scan re-creates keyed by BulkVS callID.
    Safe to run repeatedly; a no-op once clean.
    """
    async with SessionLocal() as db:
        result = await db.execute(
            delete(CallCharge).where(
                (CallCharge.kind == billing.KIND_CNAM)
                | (CallCharge.rate_code.in_([
                    billing.OUTBOUND_DOMESTIC,
                    *[f"{billing.INBOUND_TIER_PREFIX}{t}" for t in
                      ("0", "10", "1", "2", "3", "4", "AK", "PRI", "5", "6")],
                    billing.INBOUND_TOLLFREE,
                ]))
            )
        )
        await db.commit()
    removed = result.rowcount or 0
    if removed:
        logger.info("billing: purged %s superseded estimated charge rows", removed)
    return removed


async def reconcile_charges(window_hours: int | None = None) -> int:
    """Pull BulkVS's rated records for the recent window and record them. Idempotent."""
    hours = window_hours or settings.BILLING_WINDOW_HOURS
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)

    try:
        records = await bulkvs_client.fetch_voice_cdr(
            int(start.timestamp()), int(now.timestamp()), "all"
        )
    except Exception as exc:  # noqa: BLE001 - carrier outage/creds -> skip, retry next tick
        logger.warning("billing: /voice fetch failed: %s", exc)
        return 0

    if not records:
        return 0

    written = 0
    unrated = 0
    async with SessionLocal() as db:
        numbers = await _load_numbers(db)
        provider = (
            await db.execute(select(Provider).where(Provider.name == PROVIDER_NAME))
        ).scalar_one_or_none()
        provider_id = provider.id if provider is not None else None

        for rec in records:
            charge = billing.parse_voice_record(rec, settings.BULKVS_CDR_TZ)
            if charge is None:
                continue
            if charge.unrated:
                unrated += 1

            number = _number_for(charge, numbers)
            linkedid, call_id = await _correlate(db, charge, provider_id)

            values = {
                "uniqueid": charge.call_ref,
                # No grouping key exists in the /voice feed, so an uncorrelated record stands
                # alone under its own reference rather than pretending to join a call.
                "linkedid": linkedid or charge.call_ref,
                "kind": billing.KIND_MINUTES,
                "call_id": call_id,
                "number_id": number.id if number is not None else None,
                "direction": charge.direction,
                "channel": charge.trunk_group,
                "src": charge.source,
                "dst": charge.destination,
                "dest_unknown": False,  # /voice always records the real destination
                "started_at": charge.started_at,
                "raw_billsec": charge.duration_seconds,
                "billed_seconds": charge.billed_seconds,
                # rate_code marks the SOURCE of the figure, not a row in our rate table —
                # these are BulkVS's own numbers.
                "rate_code": f"bulkvs.{charge.direction}",
                "rate_amount": charge.per_minute,
                "increment_seconds": None,
                "amount": charge.amount or 0,
                "unrated": charge.unrated,
                "unrated_reason": charge.unrated_reason,
            }
            result = await db.execute(
                pg_insert(CallCharge)
                .values(id=_uuid.uuid4(), **values)
                # BulkVS's figure is definitive, so a restated record overwrites ours.
                .on_conflict_do_update(
                    index_elements=["uniqueid", "kind"],
                    set_={k: v for k, v in values.items() if k not in ("uniqueid", "kind")},
                )
            )
            written += result.rowcount or 0

        await db.commit()

    logger.info(
        "billing: %s BulkVS records over last %sh -> %s charge rows (%s unrated)",
        len(records), hours, written, unrated,
    )
    return written
