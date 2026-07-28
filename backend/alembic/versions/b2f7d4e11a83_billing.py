"""BulkVS cost-estimation tables (Billing tab)

Additive only — no existing table's DATA is touched.

  - billing_rates       the price sheet as DATA (seeded from app.services.billing.SEED_RATES,
                        which is also what the kernel resolves against — one source of truth).
                        `source` records provenance: 'sheet' = read off the operator's BulkVS
                        portal, 'web' = filled from public pricing where the portal linked out.
  - call_charges        ONE ROW PER BILLABLE LEG (per charge kind). The rate is STAMPED onto
                        the row at costing time, exactly like campaign_id is stamped onto
                        calls at ingest (ARCHITECTURE.md #1), so changing a rate later never
                        silently rewrites what last month cost.
  - billing_adjustments manual account-level charges with no call data behind them (LNP port
                        fees, E911 overage, LIDB updates, directory listings).

`numbers` gains the BulkVS /tnRecord "TN Details" fields the sync already had access to but
never stored — chiefly `tier`, which selects the inbound rate (the sheet spans $0.0003 to
$0.0198 per minute, so tier accuracy dominates the whole estimate).

NOTE: the Asterisk `cdr` table this feature READS is owned by Asterisk (cdr_pgsql), not by
Alembic — it is deliberately neither created nor altered here.

Revision ID: b2f7d4e11a83
Revises: a4d7e91c3b06
Create Date: 2026-07-28 23:10:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'b2f7d4e11a83'
down_revision = 'a4d7e91c3b06'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- price sheet as data ---------------------------------------------------------------
    op.create_table(
        'billing_rates',
        sa.Column('code', sa.String(), primary_key=True),
        sa.Column('label', sa.String(), nullable=False),
        # per_minute | per_event | per_month
        sa.Column('unit', sa.String(), nullable=False),
        sa.Column('amount', sa.Numeric(12, 6), nullable=False),
        # Billing increment, per-minute rows only. BulkVS bills 6-second increments with a
        # 6-second minimum; kept per-row so a single invoice check can correct it without a
        # deploy, and so historical charges keep the increment they were costed under.
        sa.Column('increment_seconds', sa.Integer(), nullable=True),
        sa.Column('minimum_seconds', sa.Integer(), nullable=True),
        # Published on the inbound tier rows; informational (a port-in is a manual adjustment).
        sa.Column('lnp_fee', sa.Numeric(12, 4), nullable=True),
        # 'sheet' (operator's portal price table) | 'web' (public BulkVS pricing)
        sa.Column('source', sa.String(), nullable=False, server_default='sheet'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # --- one row per billable leg ----------------------------------------------------------
    op.create_table(
        'call_charges',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        # Asterisk CDR identity. uniqueid is the leg; linkedid groups the legs of one call.
        sa.Column('uniqueid', sa.String(), nullable=False),
        sa.Column('linkedid', sa.String(), nullable=False),
        # 'minutes' | 'cnam' — a leg can carry more than one charge.
        sa.Column('kind', sa.String(), nullable=False, server_default='minutes'),
        sa.Column('call_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('calls.id'), nullable=True),
        sa.Column('number_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('numbers.id'), nullable=True),
        sa.Column('direction', sa.String(), nullable=False),
        sa.Column('channel', sa.String(), nullable=True),
        sa.Column('src', sa.String(), nullable=True),
        sa.Column('dst', sa.String(), nullable=True),
        # True when CDR did not record a usable destination (flow-dial legs carry dst='s').
        sa.Column('dest_unknown', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('raw_billsec', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('billed_seconds', sa.Integer(), nullable=False, server_default='0'),
        # --- stamped at costing time; never recomputed -------------------------------------
        sa.Column('rate_code', sa.String(), nullable=True),
        sa.Column('rate_amount', sa.Numeric(12, 6), nullable=True),
        sa.Column('increment_seconds', sa.Integer(), nullable=True),
        sa.Column('amount', sa.Numeric(12, 6), nullable=False, server_default='0'),
        # An unrated leg is recorded with amount 0 but flagged, so the UI can report "N legs
        # unrated" instead of quietly under-reporting the bill.
        sa.Column('unrated', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('unrated_reason', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        # Idempotency: re-running the reconciler never double-bills a leg.
        sa.UniqueConstraint('uniqueid', 'kind', name='uq_charge_leg_kind'),
    )
    op.create_index('ix_call_charges_started_at', 'call_charges', ['started_at'])
    op.create_index('ix_call_charges_linkedid', 'call_charges', ['linkedid'])
    op.create_index('ix_call_charges_number_started', 'call_charges', ['number_id', 'started_at'])

    # --- manual account-level charges -------------------------------------------------------
    op.create_table(
        'billing_adjustments',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('occurred_on', sa.Date(), nullable=False),
        sa.Column('code', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('amount', sa.Numeric(12, 4), nullable=False),
        sa.Column('number_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('numbers.id'), nullable=True),
        sa.Column('created_by_user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_billing_adjustments_occurred_on', 'billing_adjustments', ['occurred_on'])

    # --- BulkVS /tnRecord "TN Details", mirrored like Status/ReferenceID already are --------
    op.add_column('numbers', sa.Column('tier', sa.String(), nullable=True))
    op.add_column('numbers', sa.Column('state', sa.String(), nullable=True))
    op.add_column('numbers', sa.Column('rate_center', sa.String(), nullable=True))
    op.add_column('numbers', sa.Column('activation_date', sa.DateTime(timezone=True), nullable=True))
    op.add_column('numbers', sa.Column('cnam_enabled', sa.Boolean(), nullable=False,
                                       server_default=sa.false()))
    # E911 is NOT reported by /tnRecord and cannot be discovered from any BulkVS endpoint, so
    # it is an operator-set per-number flag. Defaults OFF (this account has no E911).
    op.add_column('numbers', sa.Column('e911_enabled', sa.Boolean(), nullable=False,
                                       server_default=sa.false()))

    # --- seed the price sheet ---------------------------------------------------------------
    # Imported from the kernel rather than duplicated, so the seeded rows and the codes the
    # resolver produces can never drift apart.
    from app.services.billing import (
        DEFAULT_INCREMENT_SECONDS,
        DEFAULT_MINIMUM_SECONDS,
        SEED_RATES,
    )

    rates = sa.table(
        'billing_rates',
        sa.column('code', sa.String),
        sa.column('label', sa.String),
        sa.column('unit', sa.String),
        sa.column('amount', sa.Numeric),
        sa.column('increment_seconds', sa.Integer),
        sa.column('minimum_seconds', sa.Integer),
        sa.column('lnp_fee', sa.Numeric),
        sa.column('source', sa.String),
    )
    op.bulk_insert(rates, [
        {
            'code': r['code'],
            'label': r['label'],
            'unit': r['unit'],
            'amount': r['amount'],
            'increment_seconds': DEFAULT_INCREMENT_SECONDS if r['unit'] == 'per_minute' else None,
            'minimum_seconds': DEFAULT_MINIMUM_SECONDS if r['unit'] == 'per_minute' else None,
            'lnp_fee': r.get('lnp_fee'),
            'source': r.get('source', 'sheet'),
        }
        for r in SEED_RATES
    ])


def downgrade() -> None:
    op.drop_column('numbers', 'e911_enabled')
    op.drop_column('numbers', 'cnam_enabled')
    op.drop_column('numbers', 'activation_date')
    op.drop_column('numbers', 'rate_center')
    op.drop_column('numbers', 'state')
    op.drop_column('numbers', 'tier')
    op.drop_index('ix_billing_adjustments_occurred_on', table_name='billing_adjustments')
    op.drop_table('billing_adjustments')
    op.drop_index('ix_call_charges_number_started', table_name='call_charges')
    op.drop_index('ix_call_charges_linkedid', table_name='call_charges')
    op.drop_index('ix_call_charges_started_at', table_name='call_charges')
    op.drop_table('call_charges')
    op.drop_table('billing_rates')
