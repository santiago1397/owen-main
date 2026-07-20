"""inbound email relay status

Add relay_status / relay_error to inbound_emails so the log UI can show the truthful
outcome of each GHL relay attempt (sent / skipped-not-configured / failed) instead of
inferring it from relayed_to_ghl alone.

Revision ID: f3c8d2b6a1e5
Revises: e7b2c1a9f4d3
Create Date: 2026-07-20 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'f3c8d2b6a1e5'
down_revision = 'e7b2c1a9f4d3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('inbound_emails', sa.Column('relay_status', sa.String(), nullable=True))
    op.add_column('inbound_emails', sa.Column('relay_error', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('inbound_emails', 'relay_error')
    op.drop_column('inbound_emails', 'relay_status')
