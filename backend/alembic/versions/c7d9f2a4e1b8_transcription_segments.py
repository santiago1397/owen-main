"""transcription segments (dual-channel speaker separation)

Adds `transcriptions.segments` JSONB — speaker-labeled, time-ordered segments
from dual-channel recordings. NULL for mono transcripts (unchanged behavior).

Revision ID: c7d9f2a4e1b8
Revises: b4e1a7c92f10
Create Date: 2026-07-17 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'c7d9f2a4e1b8'
down_revision = 'b4e1a7c92f10'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'transcriptions',
        sa.Column('segments', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('transcriptions', 'segments')
