"""flows + flow_versions (call-flow graph schema)

Additive schema for the BulkVS+Asterisk platform (Ticket 02): an append-only flow envelope
(`flows`) pointing at immutable graph snapshots (`flow_versions.graph` jsonb). Append-only
by construction — new versions are only ever INSERTed; the only mutation is the flows
`active_version_id` pointer, moved on activation. No existing table is touched.

Revision ID: b8f4c1e6a37d
Revises: a1d4e7b2c9f6
Create Date: 2026-07-22 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'b8f4c1e6a37d'
down_revision = 'a1d4e7b2c9f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `flows` first, without the self-referential active_version_id FK (added last to
    # break the flows <-> flow_versions circular dependency).
    op.create_table(
        'flows',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('active_version_id', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'flow_versions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('flow_id', sa.UUID(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('graph', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['flow_id'], ['flows.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('flow_id', 'version', name='uq_flow_version'),
    )
    op.create_index(op.f('ix_flow_versions_flow_id'), 'flow_versions', ['flow_id'], unique=False)
    op.create_foreign_key(
        'fk_flows_active_version', 'flows', 'flow_versions', ['active_version_id'], ['id']
    )


def downgrade() -> None:
    op.drop_constraint('fk_flows_active_version', 'flows', type_='foreignkey')
    op.drop_index(op.f('ix_flow_versions_flow_id'), table_name='flow_versions')
    op.drop_table('flow_versions')
    op.drop_table('flows')
