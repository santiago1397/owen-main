"""inbound email relay result

Add inbound_emails.relay_result (jsonb) to record what the direct GHL API relay created
(contact id, opportunity id, mode) for display in the Email Log.

Revision ID: a1d4e7b2c9f6
Revises: f3c8d2b6a1e5
Create Date: 2026-07-20 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = 'a1d4e7b2c9f6'
down_revision = 'f3c8d2b6a1e5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('inbound_emails', sa.Column('relay_result', JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column('inbound_emails', 'relay_result')
