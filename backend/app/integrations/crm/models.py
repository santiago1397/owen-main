"""The `crm_links` table — WHICH DID is bound to the CRM, and what it rings.

One row per bound number. Everything about routing a CRM-linked call is data in this row,
so the owner changes which number is linked, and what it rings, with a `crm-link bind`
command and no deploy (the explicit requirement behind this table existing at all).

Deliberately NOT columns on `numbers`: `numbers` is the platform's own table, written by
the BulkVS inventory sync and read by ingestion, billing, the Inbox and the flow runtime.
Adding integration-specific columns to it would put this module inside the blast radius of
every one of those, and would make "turn the integration off" a schema question. A separate
table with an FK is removable in one migration and invisible to everything that does not
join to it.

Secrets are NOT here. `crm_token_env` names an environment variable; the value lives in
`.env.prod`. A `ghl_pat_...` in a database column would be readable by `/api/ai/query`'s
`owen_ro` role and would ride into every `pg_dump`.
"""

import uuid
from datetime import datetime

from sqlalchemy import (Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint,
                        false, func, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class CrmLink(Base):
    """A DID bound to the CRM, plus its hybrid ring group.

    `enabled` is the PER-NUMBER switch, under the global `CRM_LINK_ENABLED` kill switch.
    Both must be true for anything here to affect a call. It defaults to FALSE with a
    server_default so a row can be created, reviewed and only then switched on — binding a
    live business number should take two deliberate steps, not one.
    """

    __tablename__ = "crm_links"
    __table_args__ = (UniqueConstraint("number_id", name="uq_crm_link_number"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)

    # One binding per number. UNIQUE, so "which CRM link owns this DID?" can never have two
    # answers and a double-bind is a database error rather than a coin flip at call time.
    # ON DELETE CASCADE, and that is a safety decision rather than a convenience one. The FK
    # points FROM this table TO `numbers`, so it constrains this table — but with the default
    # NO ACTION, a `DELETE FROM numbers` that works today would newly FAIL because of a row
    # here. Nothing hard-deletes a number at present (the BulkVS sync is add-only +
    # soft-release), but an integration must not be able to make an existing operation start
    # erroring. CASCADE means deleting a number simply drops its now-meaningless binding.
    number_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("numbers.id", ondelete="CASCADE"), index=True
    )

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())

    # --- the hybrid ring group --------------------------------------------------------
    # Ring the CRM browser softphones as well as the PSTN numbers. False rings PSTN only.
    ring_operators: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    # Explicit operator ids (emails) to ring. EMPTY LIST = every operator whose softphone is
    # currently REGISTERED, which is the presence rule DEFAULT_CALL_HANDLING_SPEC decision #1
    # already established and the InCallBar availability toggle already drives. Naming
    # operators here is the escape hatch for "this DID only rings the CRM desk".
    operator_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # Up to `CRM_LINK_MAX_PSTN_LEGS` (2) E.164 numbers rung in parallel with the softphones.
    # Every entry is re-checked against CRM_LINK_ALLOWLIST at call time — a number in this
    # column is a routing intent, never a permission.
    pstn_numbers: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # How long the whole group rings before the call rolls to voicemail. NULL = fall back to
    # CRM_LINK_RING_TIMEOUT_SECONDS.
    ring_timeout_seconds: Mapped[int | None] = mapped_column(Integer)

    # --- the CRM this number reports to -----------------------------------------------
    # NULL = the global CRM_LINK_BASE_URL. Per-row so a second CRM (or a staging one) is a
    # data change, matching the "not GHL-shaped" rule in docs/CRM_CONTEXT_SPEC.md C2.
    crm_base_url: Mapped[str | None] = mapped_column(String)
    # The NAME of the env var holding this CRM's machine token, never the token itself.
    # NULL = CRM_LINK_TOKEN.
    crm_token_env: Mapped[str | None] = mapped_column(String)

    # Which operator identity a CRM-INITIATED outbound call rings first (the softphone that
    # gets bridged to the callee). NULL = the request must name one.
    outbound_operator: Mapped[str | None] = mapped_column(String)

    # Free-form operator note. Not read by any code path.
    note: Mapped[str | None] = mapped_column(String)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
