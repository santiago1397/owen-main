"""numbers.provider_status — carrier-reported DID status mirror (BulkVS /tnRecord Status).

A BulkVS DID that is still provisioning (e.g. Status="SUBMITTED" for a pending port-in)
must not be usable for any operation until the carrier reports it Active. The sync mirrors
/tnRecord's `Status` verbatim into this column; NULL (legacy Twilio/SignalWire rows and
pre-migration rows) is treated as active so nothing existing is locked out.

Revision ID: f1a6d3c8b2e9
Revises: e8a3c5f1d902
Create Date: 2026-07-23
"""

import sqlalchemy as sa
from alembic import op

revision = 'f1a6d3c8b2e9'
down_revision = 'e8a3c5f1d902'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('numbers', sa.Column('provider_status', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('numbers', 'provider_status')
