"""call_captures — structured data an AI agent collected during a call

AI_AGENT_SPEC D7. Append-only, ONE ROW PER CAPTURE EVENT: an agent may capture twice in a
call (a name early, the problem details later) and both matter, with their timestamps.

Why not the three existing homes that look like they would do:
  - call_analysis.tags   is UNIQUE per call and owned by the `analyze` job. An agent writing
                         during the call and analyze upserting after it is a write race on
                         one row, and it would lose which agent version captured what.
  - callers.label        is documented as a MANUAL override. The platform holds the line that
                         humans win over models everywhere else; an agent overwriting a
                         human-entered name inverts that.
  - contact_notes        free text, human-facing, useless for querying.

Revision ID: a1c4e7f2b830
Revises: d3b8f6e21a47
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "a1c4e7f2b830"
down_revision = "d3b8f6e21a47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "call_captures",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("call_id", UUID(as_uuid=True), sa.ForeignKey("calls.id"), nullable=False),
        # WHICH agent config produced this. Free from version pinning, and it turns "agent v3
        # started capturing garbage after I changed the prompt" into a query.
        sa.Column(
            "agent_version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agent_versions.id"),
            nullable=True,
        ),
        sa.Column("capture_type", sa.String(), nullable=False, server_default="lead"),
        sa.Column("fields", JSONB(), nullable=True),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Same relay-once guard shape as messages/inbound_emails/calls, so captured data can
        # ride the EXISTING call_relay_ghl handler instead of needing a new relay path.
        sa.Column("relayed_to_ghl", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("relayed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_call_captures_call", "call_captures", ["call_id"])
    op.create_index("ix_call_captures_agent_version", "call_captures", ["agent_version_id"])
    op.create_index("ix_call_captures_captured_at", "call_captures", ["captured_at"])


def downgrade() -> None:
    op.drop_index("ix_call_captures_captured_at", table_name="call_captures")
    op.drop_index("ix_call_captures_agent_version", table_name="call_captures")
    op.drop_index("ix_call_captures_call", table_name="call_captures")
    op.drop_table("call_captures")
