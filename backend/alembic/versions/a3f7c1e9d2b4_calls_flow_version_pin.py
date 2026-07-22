"""calls: pin the ARI-run flow_version onto the call (Ticket 07)

Additive column `calls.flow_version_id` (FK flow_versions.id, nullable, indexed). The
in-memory ARI flow interpreter pins the flow's ACTIVE version onto the call at StasisStart —
exactly like `campaign_id` is pinned at ingest — so downstream projection/analysis can
attribute which graph version handled the call.

NULL for every call that ran no assigned flow: all legacy Twilio/SignalWire calls and any
Asterisk DID without a flow assignment. No existing column is altered, no row is touched.

Revision ID: a3f7c1e9d2b4
Revises: c9a2f1d4b7e3
Create Date: 2026-07-22 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'a3f7c1e9d2b4'
down_revision = 'c9a2f1d4b7e3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('calls', sa.Column('flow_version_id', sa.UUID(), nullable=True))
    op.create_index('ix_calls_flow_version_id', 'calls', ['flow_version_id'])
    op.create_foreign_key(
        'fk_calls_flow_version_id', 'calls', 'flow_versions', ['flow_version_id'], ['id']
    )


def downgrade() -> None:
    op.drop_constraint('fk_calls_flow_version_id', 'calls', type_='foreignkey')
    op.drop_index('ix_calls_flow_version_id', table_name='calls')
    op.drop_column('calls', 'flow_version_id')
