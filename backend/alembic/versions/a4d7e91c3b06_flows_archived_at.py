"""Flow library management: soft-delete (archive) for flows

Adds one nullable timestamp to `flows`, written ONLY by user actions:
  - archived_at — hides the flow from the library list (and from the Numbers flow picker)
    without deleting anything. Needed because a flow can NOT generally be hard-deleted:
    calls.flow_version_id references flow_versions for call attribution, and every FK in
    the chain is NO ACTION, so deleting a flow that has ever handled a call would either
    violate the constraint or destroy call history. DELETE /api/flows/{id} hard-deletes
    only when no call references any of the flow's versions, and archives otherwise.

Additive and backwards-compatible (nullable column, no data touched), so it is safe to
apply before the app image that reads it is deployed.

Revision ID: a4d7e91c3b06
Revises: f9c2a1b7e4d6
Create Date: 2026-07-28 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'a4d7e91c3b06'
down_revision = 'f9c2a1b7e4d6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('flows', sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('flows', 'archived_at')
