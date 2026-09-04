"""agent_slots — a named, swappable pointer to an agent

AI_AGENT_SPEC D12. An `ai_agent` node may reference a SLOT ("receptionist") instead of a
concrete agent, so swapping which agent answers is a data edit rather than a new flow
version. Flow-version history then keeps meaning "the routing changed" instead of "someone
tried a different agent", and A/B testing becomes possible.

Pinning is unaffected: the concrete `agent_version_id` is still resolved and pinned onto the
call, so every past call remains attributable regardless of where the slot points today.

Third application of a pattern already trusted twice (`flows.active_version_id`,
`agents.active_version_id`): a mutable pointer in front of immutable content.

Revision ID: b2d5f8a3c914
Revises: a1c4e7f2b830
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "b2d5f8a3c914"
down_revision = "a1c4e7f2b830"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_slots",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("agent_slots")
