"""inbound emails

Add the inbound_emails table: job-notification emails pulled from the Hostinger mailbox
over IMAP, deduped on the RFC Message-ID, with the raw email always stored and a
parse_status that gates the GHL relay (only 'parsed' rows are relayed).

Revision ID: e7b2c1a9f4d3
Revises: d5f1a9c3e2b7
Create Date: 2026-07-20 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = 'e7b2c1a9f4d3'
down_revision = 'd5f1a9c3e2b7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'inbound_emails',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('message_id', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=True),
        sa.Column('from_addr', sa.String(), nullable=True),
        sa.Column('to_addr', sa.String(), nullable=True),
        sa.Column('subject', sa.String(), nullable=True),
        sa.Column('job_id', sa.String(), nullable=True),
        sa.Column('parse_status', sa.String(), nullable=False, server_default='failed'),
        sa.Column('parse_error', sa.Text(), nullable=True),
        sa.Column('fields', JSONB(), nullable=True),
        sa.Column('raw', sa.Text(), nullable=True),
        sa.Column('relayed_to_ghl', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('relayed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('message_id', name='uq_inbound_email_message_id'),
    )
    op.create_index('ix_inbound_emails_job_id', 'inbound_emails', ['job_id'])
    op.create_index('ix_inbound_emails_parse_status', 'inbound_emails', ['parse_status'])
    op.create_index(
        'ix_inbound_emails_source_received', 'inbound_emails', ['source', 'received_at']
    )


def downgrade() -> None:
    op.drop_index('ix_inbound_emails_source_received', table_name='inbound_emails')
    op.drop_index('ix_inbound_emails_parse_status', table_name='inbound_emails')
    op.drop_index('ix_inbound_emails_job_id', table_name='inbound_emails')
    op.drop_table('inbound_emails')
