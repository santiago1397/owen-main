"""call_charges: AI cost, marked DERIVED rather than rated

AI_AGENT_SPEC D14. STT/LLM/TTS cost goes in `call_charges` alongside carrier minutes, because
the question worth answering is "what did this call cost me, all in?" — and that is one query
only if they share a table, which `/billing` already aggregates.

The provenance column is not pedantry. `services/billing.py` opens with "USAGE COST IS NOT
ESTIMATED", because BulkVS publishes rated CDRs and an earlier design that estimated from
Asterisk's CDR was found wrong in BOTH directions. AI vendors issue no per-call charge: cost
is computed from usage they report against published rates. Conflating derived with rated is
exactly the mistake that module was rewritten to stop making, so the two are distinguishable
by column rather than by convention.

`agent_version_id` makes "version 7 doubled our token spend" a query rather than an
investigation — free, since the call already pins it.

Revision ID: c3e6a9d1f725
Revises: b2d5f8a3c914
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "c3e6a9d1f725"
down_revision = "b2d5f8a3c914"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 'rated' = the carrier issued this figure. 'derived' = we computed it from usage a vendor
    # reported. Existing rows are carrier CDR projections, so they are rated by definition.
    op.add_column(
        "call_charges",
        sa.Column("provenance", sa.String(), nullable=False, server_default="rated"),
    )
    op.add_column(
        "call_charges",
        sa.Column("agent_version_id", UUID(as_uuid=True),
                  sa.ForeignKey("agent_versions.id"), nullable=True),
    )
    # Usage as the vendor reported it (tokens, audio seconds, characters), kept verbatim so a
    # rate correction can be re-applied later without re-running any calls.
    op.add_column("call_charges", sa.Column("usage", sa.dialects.postgresql.JSONB(), nullable=True))
    op.create_index("ix_call_charges_provenance", "call_charges", ["provenance"])
    op.create_index("ix_call_charges_agent_version", "call_charges", ["agent_version_id"])


def downgrade() -> None:
    op.drop_index("ix_call_charges_agent_version", table_name="call_charges")
    op.drop_index("ix_call_charges_provenance", table_name="call_charges")
    op.drop_column("call_charges", "usage")
    op.drop_column("call_charges", "agent_version_id")
    op.drop_column("call_charges", "provenance")
