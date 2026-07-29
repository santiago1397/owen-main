"""Mark BulkVS numbers that arrived by PORT rather than purchase

Additive. A ported-in US number costs $0.00 (BulkVS ports are free); a number bought from
inventory costs a $0.05 setup fee. Without knowing which is which, the Billing tab charges
every number a setup fee and overstates one-time costs — on this account by exactly the
$0.05 phantom fee on the ported DID.

The flag is mirrored from BulkVS `/portTn`, so it is carrier truth rather than a guess.

Revision ID: c4a9e2f70b15
Revises: b2f7d4e11a83
Create Date: 2026-07-29 01:05:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'c4a9e2f70b15'
down_revision = 'b2f7d4e11a83'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('numbers', sa.Column('ported_in', sa.Boolean(), nullable=False,
                                       server_default=sa.false()))
    # Correct the DID setup fee. It was seeded at $0.25 from a widely-cited third-party
    # review; BulkVS's own pricing page states $0.05 for US origination numbers, and $0.25
    # does not fit this account's observed balance.
    op.execute("UPDATE billing_rates SET amount = 0.05 WHERE code = 'did.setup'")
    op.execute("UPDATE billing_rates SET label = 'DID setup fee (purchased numbers only)' "
               "WHERE code = 'did.setup'")
    op.execute("UPDATE billing_rates SET label = 'CNAM lookup (per inbound call)' "
               "WHERE code = 'cnam.dip'")


def downgrade() -> None:
    op.execute("UPDATE billing_rates SET amount = 0.25 WHERE code = 'did.setup'")
    op.drop_column('numbers', 'ported_in')
