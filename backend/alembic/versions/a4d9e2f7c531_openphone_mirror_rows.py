"""openphone_mirror_rows — what the OpenPhone mirror has already handed to the CRM

Purely additive. It creates ONE new table and touches no existing table's schema or data,
so applying it changes the behaviour of nothing: nothing in the platform joins to this
table, and with the mirror's kill switch off (the default) nothing writes to it either.

Every non-nullable column carries a `server_default`, so this is safe to apply to a live
database with the old code still running — the old code does not know the table exists.

The UNIQUE on (kind, external_id) is the point of the table. It is the cheap half of the
mirror's idempotency: it stops a five-minute poll re-pushing the same call 288 times a day.
The guarantee proper lives on the CRM side, where `conversation_events.dedupe_key` is UNIQUE
and a repeat ingest returns the existing row — that is the half that survives a worker
timeout between the CRM's commit and its response.

Dropping this table is how the mirror is removed. Nothing else has to change.

Revision ID: a4d9e2f7c531
Revises: e1c7b4a90d63
Create Date: 2026-09-11 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a4d9e2f7c531'
down_revision = 'e1c7b4a90d63'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'openphone_mirror_rows',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        # "call" | "message". A plain string, not an enum: a third kind should not need a
        # migration on a live phone system.
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('external_id', sa.String(length=120), nullable=False),
        sa.Column('dedupe_key', sa.String(length=200), nullable=False),
        # Last ten digits only. No customer's full number, name, message body or transcript
        # is stored here — those pass THROUGH to the CRM, which is their system of record.
        # A second copy in OWEN would put customer correspondence in every pg_dump and
        # within reach of the owen_ro role behind /api/ai/query, for no benefit.
        sa.Column('customer_key', sa.String(length=20), nullable=True),
        sa.Column('line_number', sa.String(length=40), nullable=True),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('pushed_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('attempts', sa.Integer(), server_default='1', nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('kind', 'external_id', name='uq_openphone_mirror_object'),
    )
    op.create_index('ix_openphone_mirror_occurred', 'openphone_mirror_rows',
                    ['occurred_at'], unique=False)
    op.create_index(op.f('ix_openphone_mirror_rows_customer_key'),
                    'openphone_mirror_rows', ['customer_key'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_openphone_mirror_rows_customer_key'),
                  table_name='openphone_mirror_rows')
    op.drop_index('ix_openphone_mirror_occurred', table_name='openphone_mirror_rows')
    op.drop_table('openphone_mirror_rows')
