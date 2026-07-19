"""call ghl relay flags

Add relayed_to_ghl / relayed_at to calls so completed-call summaries relay to GHL
exactly once (mirrors the messages table's relay guard).

Revision ID: d5f1a9c3e2b7
Revises: c7d9f2a4e1b8
Create Date: 2026-07-19 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'd5f1a9c3e2b7'
down_revision = 'c7d9f2a4e1b8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'calls',
        sa.Column('relayed_to_ghl', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'calls',
        sa.Column('relayed_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('calls', 'relayed_at')
    op.drop_column('calls', 'relayed_to_ghl')
