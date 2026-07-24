"""Quo-style Inbox right-click actions: per-contact block + soft-delete

Additive schema for the Inbox thread context menu (/inbox). Adds two nullable timestamps to
contact_thread_state, written ONLY by user actions:
  - deleted_at — soft-hides the thread; AUTO-REAPPEARS on activity newer than it (derived,
    like closed_at), so a new inbound message/call surfaces the contact again.
  - blocked_at — hides the thread AND gates outbound (send + call refuse a blocked contact);
    does NOT auto-reappear. Inbound stays store-but-hide (ingestion untouched).
No existing table's DATA is touched.

Revision ID: f9c2a1b7e4d6
Revises: f1a6d3c8b2e9
Create Date: 2026-07-24 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'f9c2a1b7e4d6'
down_revision = 'f1a6d3c8b2e9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('contact_thread_state', sa.Column('blocked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('contact_thread_state', sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('contact_thread_state', 'deleted_at')
    op.drop_column('contact_thread_state', 'blocked_at')
