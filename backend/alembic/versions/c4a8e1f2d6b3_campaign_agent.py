"""campaign agent — an AI agent per campaign, and the campaign's brief for it

Purely additive: TWO nullable ADD COLUMNs on `campaigns` and nothing else. `agent_id` is a
nullable FK to `agents`; `agent_brief` is free text. No existing column is altered, nothing
is dropped, no row is written, so every existing campaign reads NULL in both — which is
exactly "this campaign names no agent", and routing is unchanged until an operator sets one.

Revision ID: c4a8e1f2d6b3
Revises: b7e2c4d91f35
Create Date: 2026-09-25 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = 'c4a8e1f2d6b3'
down_revision = 'b7e2c4d91f35'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('campaigns', sa.Column('agent_id', UUID(as_uuid=True), nullable=True))
    op.create_foreign_key('fk_campaigns_agent_id', 'campaigns', 'agents',
                          ['agent_id'], ['id'])
    op.add_column('campaigns', sa.Column('agent_brief', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('campaigns', 'agent_brief')
    op.drop_constraint('fk_campaigns_agent_id', 'campaigns', type_='foreignkey')
    op.drop_column('campaigns', 'agent_id')
