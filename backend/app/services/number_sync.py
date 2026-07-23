"""BulkVS number-inventory sync (Ticket 03) — add-only mirror of /tnRecord.

The reconcile is a PURE planner (`plan_sync`) over the current `numbers` rows vs. the
incoming BulkVS DIDs, plus a thin DB applier (`apply_sync`) that executes the plan. The
planner is kept dependency-free (no sqlalchemy/config imports at module load) so the
add-only + soft-release + reactivate + label-mirror rules are proven in isolation, exactly
like app.flows.service's version kernel. The DB imports are lazy inside `apply_sync`.

Rules (locked design):
- a DID present in /tnRecord but absent from `numbers`  -> INSERT (active, identity stamped)
- a DID present + row inactive/released                 -> REACTIVATE the SAME row
                                                           (released_at cleared, active=True)
- a DID present + active, label changed                 -> RELABEL (mirror ReferenceID)
- an ACTIVE row whose DID vanished from /tnRecord        -> SOFT-RELEASE (active=False,
                                                           released_at set; row NOT deleted)

Lifecycle (available / assigned / released) is DERIVED (`derive_lifecycle`) — never stored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("number_sync")


def is_carrier_active(provider_status) -> bool:
    """True iff the carrier-reported status allows the DID to be operated (calls/SMS/etc.).

    BulkVS /tnRecord reports "Active" for an operable DID and other states (e.g. "SUBMITTED"
    for a pending port-in) while provisioning. NULL is treated as active for backward compat:
    legacy (Twilio/SignalWire) rows and rows synced before the provider_status column existed
    carry NULL and must not be locked out."""
    if provider_status is None:
        return True
    return str(provider_status).strip().lower() == "active"


def derive_lifecycle(*, active, released_at, campaign_id=None, flow_id=None,
                     provider_status=None) -> str:
    """Derived number lifecycle. Released dominates (a soft-released DID is released even if
    it still carries an old campaign/flow); a non-Active carrier status (e.g. a SUBMITTED
    port-in) makes it pending; otherwise a number with a campaign or flow is assigned, and
    an active un-assigned number is available. Lifecycle itself is never stored."""
    if released_at is not None or not active:
        return "released"
    if not is_carrier_active(provider_status):
        return "pending"
    if campaign_id is not None or flow_id is not None:
        return "assigned"
    return "available"


@dataclass
class SyncPlan:
    """What apply_sync should do this poll. `insert`/`reactivate`/`adopt` carry the incoming
    TN (so the label is known); `relabel` carries the row + its new label; `soft_release` the
    row."""

    insert: list = field(default_factory=list)        # [tn]
    reactivate: list = field(default_factory=list)     # [(row, tn)]
    adopt: list = field(default_factory=list)          # [(row, tn)]
    relabel: list = field(default_factory=list)        # [(row, new_label)]
    restatus: list = field(default_factory=list)       # [(row, new_provider_status)]
    soft_release: list = field(default_factory=list)   # [row]


def plan_sync(existing, incoming, foreign=None) -> SyncPlan:
    """Pure diff. `existing` = current bulkvs-owned Number rows (need .phone_number,
    .friendly_name, .active, .released_at); `incoming` = normalized BulkVS TNs (need
    .phone_number, .reference_id). Keys on phone_number. Duplicate incoming TNs collapse
    (last wins). Rows are inspected only, never mutated — the applier does the writes.

    `foreign` = Number rows for the same phone_number already owned by a DIFFERENT provider
    (typically a legacy Twilio/SignalWire row for a DID that has since been ported to BulkVS).
    A DID that matches one of these ADOPTS that row in place (stamps owner/media provider onto
    the SAME row, preserving its campaign_id/call history) instead of being planned as a fresh
    insert — inserting would create a second `numbers` row for one physical DID, which is
    exactly the "duplicate number" bug this guards against."""
    by_phone = {row.phone_number: row for row in existing}
    foreign_by_phone = {row.phone_number: row for row in (foreign or [])}
    incoming_by_phone = {tn.phone_number: tn for tn in incoming}

    plan = SyncPlan()
    for phone, tn in incoming_by_phone.items():
        foreign_row = foreign_by_phone.get(phone)
        if foreign_row is not None:
            plan.adopt.append((foreign_row, tn))
            continue
        row = by_phone.get(phone)
        if row is None:
            plan.insert.append(tn)
            continue
        # A released (soft-released or otherwise inactive) row that reappears reactivates the
        # SAME row and re-syncs its label + carrier status in one step.
        if not row.active or row.released_at is not None:
            plan.reactivate.append((row, tn))
            continue
        if tn.reference_id != row.friendly_name:
            # One-way label mirror: friendly_name tracks ReferenceID (including clearing it
            # back to None when the operator removes the note in the portal).
            plan.relabel.append((row, tn.reference_id))
        # One-way carrier-status mirror: provider_status tracks /tnRecord's Status verbatim
        # (e.g. SUBMITTED -> Active when a port-in completes). getattr-tolerant so older
        # duck-typed rows/TNs without the field diff as None (= no churn).
        new_status = getattr(tn, "status", None)
        if new_status != getattr(row, "provider_status", None):
            plan.restatus.append((row, new_status))

    for phone, row in by_phone.items():
        # Only ACTIVE rows soft-release on vanish; an already-released row that stays gone
        # is left untouched (idempotent across polls — no repeated released_at churn).
        if phone not in incoming_by_phone and row.active:
            plan.soft_release.append(row)

    return plan


async def apply_sync(db, records) -> dict:
    """Apply plan_sync against the DB for the BulkVS owner provider. Additive: only the
    bulkvs-owned `numbers` rows are ever touched — Twilio/SignalWire numbers are untouched.
    Returns per-action counts for logging. DB deps imported lazily so the pure kernel above
    stays importable without sqlalchemy."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.core.config import settings
    from app.models import Number
    from app.services.ingestion import _get_or_create_provider

    provider = await _get_or_create_provider(db, settings.BULKVS_OWNER_PROVIDER)
    existing = (
        await db.execute(select(Number).where(Number.provider_id == provider.id))
    ).scalars().all()
    # Rows for the same phone_number already owned by a DIFFERENT provider (e.g. a legacy
    # Twilio/SignalWire tracking number whose DID has since been ported to BulkVS). These are
    # candidates to ADOPT rather than duplicate.
    foreign = (
        await db.execute(select(Number).where(Number.provider_id != provider.id))
    ).scalars().all()

    plan = plan_sync(existing, records, foreign=foreign)
    now = datetime.now(timezone.utc)

    for row, tn in plan.adopt:
        row.owner_provider = settings.BULKVS_OWNER_PROVIDER
        row.media_provider = settings.BULKVS_MEDIA_PROVIDER
        row.active = True
        row.released_at = None
        row.provider_status = tn.status
        if tn.reference_id and tn.reference_id != row.friendly_name:
            row.friendly_name = tn.reference_id
    for tn in plan.insert:
        db.add(
            Number(
                provider_id=provider.id,
                phone_number=tn.phone_number,
                friendly_name=tn.reference_id,
                active=True,
                owner_provider=settings.BULKVS_OWNER_PROVIDER,
                media_provider=settings.BULKVS_MEDIA_PROVIDER,
                provider_status=tn.status,
            )
        )
    for row, tn in plan.reactivate:
        row.active = True
        row.released_at = None
        row.friendly_name = tn.reference_id
        row.provider_status = tn.status
        # Backfill identity in case the row predates the split-identity columns.
        row.owner_provider = settings.BULKVS_OWNER_PROVIDER
        row.media_provider = settings.BULKVS_MEDIA_PROVIDER
    for row, label in plan.relabel:
        row.friendly_name = label
    for row, new_status in plan.restatus:
        row.provider_status = new_status
    for row in plan.soft_release:
        row.active = False
        row.released_at = now

    await db.commit()

    counts = {
        "inserted": len(plan.insert),
        "reactivated": len(plan.reactivate),
        "adopted": len(plan.adopt),
        "relabeled": len(plan.relabel),
        "restatused": len(plan.restatus),
        "soft_released": len(plan.soft_release),
    }
    logger.info(
        "bulkvs number sync: inserted=%(inserted)d reactivated=%(reactivated)d "
        "adopted=%(adopted)d relabeled=%(relabeled)d restatused=%(restatused)d "
        "soft_released=%(soft_released)d", counts
    )
    return counts
