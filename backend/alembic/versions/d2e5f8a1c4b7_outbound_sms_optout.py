"""manual outbound SMS gate + opt-out (numbers.sms_enabled/sms_campaign_id,
messages.sent_by_user_id, sms_opt_outs)

Additive schema for the BulkVS+Asterisk platform (Ticket 10). Adds the per-number 10DLC
outbound gate (`numbers.sms_enabled` + `numbers.sms_campaign_id`), the manual-outbound audit
column (`messages.sent_by_user_id`), and the app-level opt-out table (`sms_opt_outs`, one row
per (number_id, contact)). No existing table's DATA is touched; the new number columns default
to "cannot send" so no number can send outbound SMS until an operator explicitly enables it.

FLAG: down_revision is the current head `b1c4e7a9d3f2` (agents + agent_versions, Ticket 11).
After tickets 03/07/11 that IS the head. If the platform migration set is re-chained during
reconcile, only this Revises line changes.

Revision ID: d2e5f8a1c4b7
Revises: b1c4e7a9d3f2
Create Date: 2026-07-22 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'd2e5f8a1c4b7'
down_revision = 'b1c4e7a9d3f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Per-number 10DLC outbound gate (default: cannot send).
    op.add_column(
        'numbers',
        sa.Column('sms_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column('numbers', sa.Column('sms_campaign_id', sa.String(), nullable=True))

    # Manual-outbound audit: which operator sent an outbound reply (NULL for inbound rows).
    op.add_column('messages', sa.Column('sent_by_user_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_messages_sent_by_user_id'), 'messages', ['sent_by_user_id'], unique=False)
    op.create_foreign_key(
        'fk_messages_sent_by_user_id', 'messages', 'users', ['sent_by_user_id'], ['id']
    )

    # App-level opt-out state per (number_id, contact).
    op.create_table(
        'sms_opt_outs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('number_id', sa.UUID(), nullable=False),
        sa.Column('contact', sa.String(), nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('last_keyword', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['number_id'], ['numbers.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('number_id', 'contact', name='uq_optout_number_contact'),
    )
    op.create_index(op.f('ix_sms_opt_outs_number_id'), 'sms_opt_outs', ['number_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_sms_opt_outs_number_id'), table_name='sms_opt_outs')
    op.drop_table('sms_opt_outs')

    op.drop_constraint('fk_messages_sent_by_user_id', 'messages', type_='foreignkey')
    op.drop_index(op.f('ix_messages_sent_by_user_id'), table_name='messages')
    op.drop_column('messages', 'sent_by_user_id')

    op.drop_column('numbers', 'sms_campaign_id')
    op.drop_column('numbers', 'sms_enabled')
