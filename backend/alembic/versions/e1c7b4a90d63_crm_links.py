"""crm_links — which DID is bound to the CRM, and what its hybrid ring group rings

Purely additive. It creates ONE new table and touches no existing table's schema or data,
so applying it changes the behaviour of nothing: with no rows in `crm_links`, every number
resolves to "not bound" and every call path runs exactly as it did before.

Every non-nullable column carries a `server_default`, so the migration is safe to apply to a
live database with the old code still running (the old code does not know these columns
exist and will never supply them).

`enabled` defaults to FALSE deliberately. Creating a binding and switching it on are two
separate, deliberate acts — the numbers involved ring in a real business.

`number_id` is UNIQUE, so a DID can never have two bindings and "which CRM link owns this
number?" cannot have two answers at call time.

Revision ID: e1c7b4a90d63
Revises: c3e6a9d1f725
Create Date: 2026-09-10 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'e1c7b4a90d63'
down_revision = 'c3e6a9d1f725'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'crm_links',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('number_id', sa.UUID(), nullable=False),
        # The per-number opt-in, under the global CRM_LINK_ENABLED kill switch.
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        # --- the hybrid ring group ---
        sa.Column('ring_operators', sa.Boolean(), nullable=False, server_default=sa.true()),
        # '[]' = every operator whose softphone is currently REGISTERED.
        sa.Column('operator_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        # Up to CRM_LINK_MAX_PSTN_LEGS E.164 numbers, re-checked against the allowlist at
        # call time — a row here is routing intent, never a permission.
        sa.Column('pstn_numbers', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column('ring_timeout_seconds', sa.Integer(), nullable=True),
        # --- the CRM this number reports to ---
        sa.Column('crm_base_url', sa.String(), nullable=True),
        # The NAME of an environment variable, never a token. A `ghl_pat_...` in a column
        # would be readable by the `owen_ro` role behind /api/ai/query and would ride into
        # every pg_dump.
        sa.Column('crm_token_env', sa.String(), nullable=True),
        sa.Column('outbound_operator', sa.String(), nullable=True),
        sa.Column('note', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        # ON DELETE CASCADE so a DELETE on `numbers` that works today cannot newly FAIL
        # because of a row in THIS table. The constraint lives on crm_links; nothing is
        # added to `numbers`.
        sa.ForeignKeyConstraint(['number_id'], ['numbers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('number_id', name='uq_crm_link_number'),
    )
    op.create_index(op.f('ix_crm_links_number_id'), 'crm_links', ['number_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_crm_links_number_id'), table_name='crm_links')
    op.drop_table('crm_links')
