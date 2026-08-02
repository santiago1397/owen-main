"""AI API: machine credentials, request audit, and captured warning-level logs

Three additive tables, no changes to anything existing:

- `api_keys`      — machine credentials for /api/ai/*. SHA-256 of a high-entropy secret;
                    the plaintext is shown once at creation and never stored.
- `api_key_usage` — one row per AI-API request, so a key you handed to an external
                    integration has an audit trail (including the SQL it ran).
- `app_logs`      — WARNING+ records mirrored from the app AND worker containers, which is
                    what makes an over-HTTP /errors view possible (Docker's json-file logs
                    live on the VPS and the app container cannot read the worker's).

Revision ID: d3b8f6e21a47
Revises: c4a9e2f70b15
Create Date: 2026-08-01 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'd3b8f6e21a47'
down_revision = 'c4a9e2f70b15'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'api_keys',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('key_prefix', sa.String(), nullable=False),
        sa.Column('key_hash', sa.String(), nullable=False),
        sa.Column('scopes', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('expires_at', sa.DateTime(timezone=True)),
        sa.Column('revoked_at', sa.DateTime(timezone=True)),
        sa.Column('last_used_at', sa.DateTime(timezone=True)),
        sa.Column('created_by_user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    # Lookup path on every authenticated request: hash -> key. Unique so an (astronomically
    # unlikely) generated collision fails loudly at insert rather than silently sharing a key.
    op.create_index('ix_api_keys_key_hash', 'api_keys', ['key_hash'], unique=True)
    op.create_index('ix_api_keys_key_prefix', 'api_keys', ['key_prefix'])

    op.create_table(
        'api_key_usage',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('api_key_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('api_keys.id', ondelete='CASCADE')),
        sa.Column('endpoint', sa.String(), nullable=False),
        sa.Column('status_code', sa.Integer(), nullable=False, server_default='200'),
        sa.Column('duration_ms', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('rows', sa.Integer()),
        sa.Column('sql', sa.Text()),
        sa.Column('error', sa.Text()),
        sa.Column('at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_api_key_usage_key_at', 'api_key_usage', ['api_key_id', 'at'])
    op.create_index('ix_api_key_usage_at', 'api_key_usage', ['at'])

    op.create_table(
        'app_logs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('service', sa.String(), nullable=False),
        sa.Column('level', sa.String(), nullable=False),
        sa.Column('logger', sa.String()),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('linkedid', sa.String()),
        sa.Column('traceback', sa.Text()),
    )
    op.create_index('ix_app_logs_at', 'app_logs', ['at'])
    op.create_index('ix_app_logs_at_level', 'app_logs', ['at', 'level'])
    op.create_index('ix_app_logs_service', 'app_logs', ['service'])
    op.create_index('ix_app_logs_level', 'app_logs', ['level'])
    op.create_index('ix_app_logs_linkedid', 'app_logs', ['linkedid'])


def downgrade() -> None:
    op.drop_table('app_logs')
    op.drop_table('api_key_usage')
    op.drop_table('api_keys')
