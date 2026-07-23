"""Quo-style Inbox: contact panel fields + per-contact thread state + notes + app settings

Additive schema for the per-contact Inbox (/inbox). Adds:
  - callers.company / callers.role — operator-edited contact-panel fields;
  - contact_thread_state — one row per caller: last_read_at / closed_at, written ONLY by
    user actions (unread + auto-reopen are derived, webhooks never write here);
  - contact_notes — timestamped free-form notes per contact;
  - app_settings — tiny global key/value store (first key: 'inbox_default_number_id',
    the Inbox's default outbound DID).
No existing table's DATA is touched.

Revision ID: e8a3c5f1d902
Revises: d2e5f8a1c4b7
Create Date: 2026-07-23 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = 'e8a3c5f1d902'
down_revision = 'd2e5f8a1c4b7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Contact-panel fields (Quo-style right pane).
    op.add_column('callers', sa.Column('company', sa.String(), nullable=True))
    op.add_column('callers', sa.Column('role', sa.String(), nullable=True))

    # Per-contact read/open state. PK = caller_id (one row per contact, global).
    op.create_table(
        'contact_thread_state',
        sa.Column('caller_id', sa.UUID(), nullable=False),
        sa.Column('last_read_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['caller_id'], ['callers.id'], ),
        sa.PrimaryKeyConstraint('caller_id'),
    )

    # Timestamped notes per contact.
    op.create_table(
        'contact_notes',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('caller_id', sa.UUID(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('created_by_user_id', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['caller_id'], ['callers.id'], ),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_contact_notes_caller_id'), 'contact_notes', ['caller_id'], unique=False)

    # Global operator-editable settings (JSONB value: future keys need no migration).
    op.create_table(
        'app_settings',
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('value', JSONB(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    op.drop_table('app_settings')
    op.drop_index(op.f('ix_contact_notes_caller_id'), table_name='contact_notes')
    op.drop_table('contact_notes')
    op.drop_table('contact_thread_state')
    op.drop_column('callers', 'role')
    op.drop_column('callers', 'company')
