"""retell calls — the registry of calls a Retell voice agent answered

Purely additive: ONE new table, `retell_calls`, and nothing else. No existing table or column
is touched and no row is written, so the migration changes nothing about a call until an
agent is switched to engine "retell" AND RETELL_API_KEY is set (docs/RETELL-PLAN.md).

Every non-nullable column carries a server_default, so the table can be created on a database
that already has traffic without a backfill. The spend setting and a number's CRM assignment
need no migration: both live in the existing `app_settings` key/value table.

Revision ID: d8e3f1a2b4c6
Revises: c4a8e1f2d6b3
Create Date: 2026-10-06 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'd8e3f1a2b4c6'
down_revision = 'c4a8e1f2d6b3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'retell_calls',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('retell_call_id', sa.String(), nullable=False),
        sa.Column('linkedid', sa.String(), nullable=False),
        sa.Column('status', sa.String(), server_default='live', nullable=False),
        sa.Column('retell_agent_id', sa.String(), server_default='', nullable=False),
        sa.Column('agent_name', sa.String(), server_default='', nullable=False),
        sa.Column('agent_version_id', sa.UUID(), nullable=True),
        sa.Column('caller_number', sa.String(), nullable=True),
        sa.Column('dialed_number', sa.String(), nullable=True),
        sa.Column('call_channel_id', sa.String(), nullable=True),
        sa.Column('retell_channel_id', sa.String(), nullable=True),
        sa.Column('bridge_id', sa.String(), nullable=True),
        sa.Column('exit_port', sa.String(), nullable=True),
        sa.Column('exit_data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('captured', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('requests', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('retell_agent_version', sa.Integer(), nullable=True),
        sa.Column('cost_cents', sa.Numeric(12, 4), nullable=True),
        sa.Column('disconnection_reason', sa.String(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('sentiment', sa.String(), nullable=True),
        sa.Column('successful', sa.Boolean(), nullable=True),
        sa.Column('ended_event_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('analyzed_event_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['agent_version_id'], ['agent_versions.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('retell_call_id'),
    )
    op.create_index(op.f('ix_retell_calls_linkedid'), 'retell_calls', ['linkedid'], unique=False)
    op.create_index(op.f('ix_retell_calls_status'), 'retell_calls', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_retell_calls_status'), table_name='retell_calls')
    op.drop_index(op.f('ix_retell_calls_linkedid'), table_name='retell_calls')
    op.drop_table('retell_calls')
