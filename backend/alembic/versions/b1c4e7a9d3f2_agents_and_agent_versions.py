"""agents + agent_versions (AI voice-agent config) + calls.agent_version_id pin

Additive schema for the BulkVS+Asterisk platform (Ticket 11), mirroring flows/flow_versions:
an append-only agent envelope (`agents`) pointing at immutable config snapshots
(`agent_versions.config` jsonb). New versions are only ever INSERTed; the only mutation is
the `agents.active_version_id` pointer, moved on activation. Also pins the agent version an
`ai_agent` flow node ran onto the call via a nullable `calls.agent_version_id` (like
`calls.flow_version_id`). No existing table's data is touched.

FLAG: down_revision is the current head `a3f7c1e9d2b4` (calls.flow_version_id pin). If the
platform migration set is re-chained during reconcile, only this Revises line changes.

Revision ID: b1c4e7a9d3f2
Revises: a3f7c1e9d2b4
Create Date: 2026-07-22 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'b1c4e7a9d3f2'
down_revision = 'a3f7c1e9d2b4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `agents` first, without the self-referential active_version_id FK (added last to break
    # the agents <-> agent_versions circular dependency — same pattern as flows).
    op.create_table(
        'agents',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('active_version_id', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'agent_versions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('agent_id', sa.UUID(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('agent_id', 'version', name='uq_agent_version'),
    )
    op.create_index(op.f('ix_agent_versions_agent_id'), 'agent_versions', ['agent_id'], unique=False)
    op.create_foreign_key(
        'fk_agents_active_version', 'agents', 'agent_versions', ['active_version_id'], ['id']
    )

    # Pin the agent version an ai_agent node ran onto the call (nullable; like flow_version_id).
    op.add_column('calls', sa.Column('agent_version_id', sa.UUID(), nullable=True))
    op.create_index('ix_calls_agent_version_id', 'calls', ['agent_version_id'])
    op.create_foreign_key(
        'fk_calls_agent_version_id', 'calls', 'agent_versions', ['agent_version_id'], ['id']
    )


def downgrade() -> None:
    op.drop_constraint('fk_calls_agent_version_id', 'calls', type_='foreignkey')
    op.drop_index('ix_calls_agent_version_id', table_name='calls')
    op.drop_column('calls', 'agent_version_id')

    op.drop_constraint('fk_agents_active_version', 'agents', type_='foreignkey')
    op.drop_index(op.f('ix_agent_versions_agent_id'), table_name='agent_versions')
    op.drop_table('agent_versions')
    op.drop_table('agents')
