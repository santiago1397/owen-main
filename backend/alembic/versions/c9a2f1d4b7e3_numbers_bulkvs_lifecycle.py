"""numbers: BulkVS split-identity + soft-release + flow assignment (Ticket 03)

Additive columns on `numbers` for the BulkVS+Asterisk platform inventory sync:
- owner_provider / media_provider : split identity (carrier that OWNS the DID vs. provider
  that carries its MEDIA). NULL for legacy Twilio/SignalWire numbers — attribution there is
  unchanged (still by provider_id + phone_number).
- released_at : soft-release marker for the add-only sync (a DID that vanishes from
  /tnRecord is deactivated + timestamped rather than deleted; history is frozen).
- flow_id : optional behaviour assignment (FK flows.id); lets the DERIVED lifecycle
  (available/assigned/released) key on it. No status enum column is added.

No existing column is altered and no existing row is touched.

Revision ID: c9a2f1d4b7e3
Revises: b8f4c1e6a37d
Create Date: 2026-07-22 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'c9a2f1d4b7e3'
down_revision = 'b8f4c1e6a37d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('numbers', sa.Column('owner_provider', sa.String(), nullable=True))
    op.add_column('numbers', sa.Column('media_provider', sa.String(), nullable=True))
    op.add_column('numbers', sa.Column('released_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('numbers', sa.Column('flow_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_numbers_flow_id', 'numbers', 'flows', ['flow_id'], ['id']
    )


def downgrade() -> None:
    op.drop_constraint('fk_numbers_flow_id', 'numbers', type_='foreignkey')
    op.drop_column('numbers', 'flow_id')
    op.drop_column('numbers', 'released_at')
    op.drop_column('numbers', 'media_provider')
    op.drop_column('numbers', 'owner_provider')
