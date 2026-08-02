"""`GET /api/ai/schema` — the database reference an AI reads before writing SQL.

Introspected live from `information_schema` rather than hand-maintained, so it can never drift
from the actual database the way a checked-in schema doc would. Hand-written prose is layered
on top for the handful of tables whose *meaning* is not deducible from their column names —
which is most of the ones that matter.

The `caveats` block is the important half. Anyone writing SQL against `calls` without knowing
about NULL `started_at` will produce a confidently wrong number, and no amount of column typing
would have warned them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ai.deps import AuthedKey, require_scope
from app.api.ai.envelope import ok
from app.api.ai.periods import PERIODS
from app.core.apikeys import SCOPE_READ
from app.core.config import settings
from app.db import get_db
from app.services.ro_role import SECRET_TABLES

router = APIRouter(prefix="/api/ai", tags=["ai"])

TABLE_NOTES: dict[str, str] = {
    "calls": (
        "One row per call, projected from call_events. THE key filter: `started_at IS NOT NULL` "
        "— rows without it are ingestion artifacts and make up the majority of the table. "
        "`campaign_id`/`number_id`/`caller_id` are stamped at ingest and never re-attributed. "
        "`is_new_for_campaign` = this caller's first call to that campaign. "
        "`duration_seconds` is NULL while a call is in flight."
    ),
    "call_events": "Append-only source of truth for call status changes. `calls` is derived from it.",
    "call_analysis": (
        "LLM verdict per call: category, tags, summary, is_spam. Human overrides live in "
        "`category_override`/`is_spam_override` and WIN — always read "
        "coalesce(category_override, category). Only calls that were recorded, transcribed and "
        "analyzed have a row here, so it covers a minority of calls."
    ),
    "transcriptions": (
        "Full call transcript text. `segments` holds speaker-labeled turns for dual-channel "
        "recordings ([Caller]/[Operator]); NULL for mono."
    ),
    "recordings": "Audio metadata. `storage_path` NULL means the file was pruned by retention; the transcript is kept forever.",
    "numbers": "Tracking phone numbers in E.164. One number belongs to one campaign, never recycled.",
    "campaigns": "Ad campaigns. Join to calls via calls.campaign_id.",
    "callers": "Distinct caller phone numbers with lifetime counters. `label` is a manual human override.",
    "messages": "SMS/MMS on tracking numbers. `direction` is inbound|outbound; `body` is the text.",
    "inbound_emails": (
        "Job-notification emails polled over IMAP (Dispatch / American Home Shield). THIS IS THE "
        "LEADS TABLE: a lead is a row with source='dispatch' AND parse_status='parsed'. Extracted "
        "data is in the `fields` JSONB (customer name, phone, address, service, brand). "
        "parse_status has THREE values: 'parsed' (a work order we read), 'failed' (a work order we "
        "could NOT read — a lost lead), and 'ignored' (not a work order at all: cancellation, note "
        "on an existing job, account mail — never a lead, never an error; do not count these as "
        "failures). Separately, relay_status='failed' means it parsed fine and GoHighLevel "
        "rejected it — a real customer who never reached the CRM."
    ),
    "call_charges": (
        "BulkVS-rated cost per billable LEG, not per call — a forwarded call is billed twice "
        "(inbound + outbound minutes), so leg counts exceed call counts by design. Rates are "
        "stamped at costing time so history never silently re-prices."
    ),
    "billing_rates": "Local price sheet; only RECURRING charges are computed from it (usage comes rated from BulkVS).",
    "billing_adjustments": "Manual account-level charges with no call behind them (port fees, E911 overage).",
    "jobs": "Durable job queue. status='failed' with attempts>=5 means dead — nothing will retry it.",
    "app_logs": "WARNING+ log records mirrored from the app and worker containers. Not retroactive.",
    "flows": "Call-flow definitions; `flow_versions` holds the published graph a call actually ran.",
    "agents": "AI voice agent configs; `agent_versions` holds the version a call actually ran.",
    "providers": "Telephony accounts (twilio / signalwire / bulkvs). Join via calls.provider_id.",
}

CAVEATS: list[str] = [
    "calls: ALWAYS filter `started_at IS NOT NULL`. Roughly 25,000 rows lack it; they are "
    "ingestion artifacts, not calls. A bare COUNT(*) on `calls` is not call volume.",
    "calls: 'junk' means duration_seconds <= 13 OR status IN "
    "('failed','busy','no-answer','canceled'). OWEN's dashboard hides these by default, so "
    "exclude them to match reported figures.",
    "call_analysis.is_spam is effectively dead data (~25 rows flagged out of 30,000+). Do not "
    "use it to measure call quality — use duration and status.",
    "Timestamps are UTC. Bucket by day with "
    f"date_trunc('day', timezone('{settings.BUSINESS_TZ}', started_at)) or daily numbers will "
    "not match the dashboard.",
    "Human overrides win: read coalesce(category_override, category) and "
    "coalesce(is_spam_override, is_spam).",
    f"SELECT on {', '.join(SECRET_TABLES)} is revoked and cannot be granted.",
]


@router.get("/schema")
async def schema(
    table: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: AuthedKey = Depends(require_scope(SCOPE_READ)),
) -> dict:
    """Tables and columns available to /api/ai/query, with the caveats that make queries correct.

    Pass `table=` to get one table's columns only; omit it for the whole database.
    """
    where = "WHERE table_schema = 'public'"
    params: dict = {}
    if table:
        where += " AND table_name = :t"
        params["t"] = table.strip().lower()

    rows = (await db.execute(text(
        f"SELECT table_name, column_name, data_type, is_nullable "
        f"FROM information_schema.columns {where} "
        f"ORDER BY table_name, ordinal_position"
    ), params)).all()

    tables: dict[str, dict] = {}
    for tname, col, dtype, nullable in rows:
        if tname in ("alembic_version", *SECRET_TABLES):
            # Secret tables are unreadable by the query role; listing them would only produce
            # queries that fail with a permission error.
            continue
        entry = tables.setdefault(tname, {"note": TABLE_NOTES.get(tname), "columns": []})
        entry["columns"].append({"name": col, "type": dtype, "nullable": nullable == "YES"})

    return ok(
        summary=f"{len(tables)} readable tables. Read `caveats` before writing any query against "
                f"`calls` — a naive COUNT(*) there is wrong by roughly 25,000 rows.",
        data={
            "tables": tables,
            "caveats": CAVEATS,
            "business_timezone": settings.BUSINESS_TZ,
            "named_periods": PERIODS,
            "unreadable_tables": list(SECRET_TABLES),
        },
        applied_filters={"table": table},
        notes=["This schema is introspected live, so it always matches the running database."],
    )
