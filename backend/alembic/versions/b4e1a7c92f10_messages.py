"""messages (inbound SMS)

Revision ID: b4e1a7c92f10
Revises: 102acd09d6ff
Create Date: 2026-07-17 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'b4e1a7c92f10'
down_revision = '102acd09d6ff'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('messages',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('provider_id', sa.Integer(), nullable=False),
    sa.Column('provider_message_sid', sa.String(), nullable=False),
    sa.Column('number_id', sa.UUID(), nullable=True),
    sa.Column('caller_id', sa.UUID(), nullable=True),
    sa.Column('campaign_id', sa.UUID(), nullable=True),
    sa.Column('direction', sa.String(), nullable=True),
    sa.Column('from_number', sa.String(), nullable=True),
    sa.Column('to_number', sa.String(), nullable=True),
    sa.Column('body', sa.Text(), nullable=True),
    sa.Column('status', sa.String(), nullable=True),
    sa.Column('num_media', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('media_urls', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('relayed_to_ghl', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('relayed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('raw_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['provider_id'], ['providers.id'], ),
    sa.ForeignKeyConstraint(['number_id'], ['numbers.id'], ),
    sa.ForeignKeyConstraint(['caller_id'], ['callers.id'], ),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('provider_message_sid')
    )
    op.create_index(op.f('ix_messages_provider_id'), 'messages', ['provider_id'], unique=False)
    op.create_index(op.f('ix_messages_number_id'), 'messages', ['number_id'], unique=False)
    op.create_index(op.f('ix_messages_caller_id'), 'messages', ['caller_id'], unique=False)
    op.create_index(op.f('ix_messages_campaign_id'), 'messages', ['campaign_id'], unique=False)
    op.create_index('ix_messages_number_received', 'messages', ['number_id', 'received_at'], unique=False)
    op.create_index('ix_messages_campaign_received', 'messages', ['campaign_id', 'received_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_messages_campaign_received', table_name='messages')
    op.drop_index('ix_messages_number_received', table_name='messages')
    op.drop_index(op.f('ix_messages_campaign_id'), table_name='messages')
    op.drop_index(op.f('ix_messages_caller_id'), table_name='messages')
    op.drop_index(op.f('ix_messages_number_id'), table_name='messages')
    op.drop_index(op.f('ix_messages_provider_id'), table_name='messages')
    op.drop_table('messages')
