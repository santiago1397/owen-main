"""inbound email crm delivery — what reached the CRM, per email

Purely additive: FOUR nullable ADD COLUMNs on `inbound_emails` and nothing else. No existing
column is altered, nothing is dropped, and no row is written. Every existing row reads NULL
in `crm_status`, which is exactly what the no-backfill guard needs: the `email_relay_crm`
job only ever acts on a row the mail poller stamped 'queued' when it first inserted it, so
an email stored before this existed can never be sent to the CRM.

All four are nullable, so no server_default is needed and the old code keeps running
against the new schema unchanged.

Revision ID: b7e2c4d91f35
Revises: a4d9e2f7c531
Create Date: 2026-09-14 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = 'b7e2c4d91f35'
down_revision = 'a4d9e2f7c531'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('inbound_emails', sa.Column('crm_status', sa.String(), nullable=True))
    op.add_column('inbound_emails', sa.Column('crm_error', sa.Text(), nullable=True))
    op.add_column('inbound_emails', sa.Column('crm_result', JSONB(), nullable=True))
    op.add_column('inbound_emails',
                  sa.Column('crm_attempted_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('inbound_emails', 'crm_attempted_at')
    op.drop_column('inbound_emails', 'crm_result')
    op.drop_column('inbound_emails', 'crm_error')
    op.drop_column('inbound_emails', 'crm_status')
