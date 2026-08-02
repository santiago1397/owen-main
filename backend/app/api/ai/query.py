"""`/api/ai/query` — arbitrary read-only SQL, for the questions no curated endpoint anticipated.

**The security boundary is the database role, not this file.** Every check below is a
convenience that produces a better error message; none of them is what stops a write. That job
belongs to `owen_ro`, a login role with SELECT and nothing else, and with SELECT explicitly
revoked on `users`, `api_keys` and `api_key_usage` (see `services/ro_role.py`). If every guard
in this module were bypassed, the worst a caller could do is read what that role can read.

That ordering matters because the alternative — a regex denylist over table names on the app's
read/write connection — loses to any attacker who can spell a table name two ways, and puts
password hashes one clever query away.

Layered on top of the role, because they make the endpoint *usable* rather than merely safe:

- one statement per request (no `;` chaining)
- an explicitly READ ONLY transaction (a belt to the role's braces)
- `statement_timeout` so a cartesian join cannot pin the box
- a row cap applied by wrapping the caller's statement in an outer SELECT, so it cannot be
  opted out of by omitting LIMIT

Requires BOTH the `sql` and `content` scopes. This is honest rather than restrictive: a role
that can SELECT can read transcripts, SMS bodies and customer addresses, so pretending `sql`
alone is content-free would be a lie told by the scope name.
"""

from __future__ import annotations

import re
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.ai.deps import AuthedKey, authenticate, record_usage
from app.api.ai.envelope import NOTE_PHANTOM, error_detail, ok
from app.core.apikeys import SCOPE_CONTENT, SCOPE_SQL
from app.core.config import settings

router = APIRouter(prefix="/api/ai", tags=["ai"])

# Statements that are unambiguously not a read. The role rejects these anyway; matching them
# here turns a raw Postgres permission error into a sentence the caller can act on.
_WRITE_WORDS = re.compile(
    r"^\s*(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|do|"
    r"vacuum|analyze|reindex|refresh|comment|security|listen|notify|lock|set|reset)\b",
    re.IGNORECASE,
)
_READ_START = re.compile(r"^\s*(select|with|table|explain|show)\b", re.IGNORECASE)

_engine = None
_session_factory = None


def _ro_sessions():
    """Lazily build the read-only engine. Small pool: this endpoint is rate-limited to a
    handful of requests a minute and must never crowd out the app's own connections."""
    global _engine, _session_factory
    if _session_factory is None:
        url = settings.readonly_database_url
        if url is None:
            return None
        _engine = create_async_engine(url, pool_size=2, max_overflow=2, pool_pre_ping=True)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _session_factory


class QueryIn(BaseModel):
    sql: str = Field(min_length=1, max_length=20000)
    limit: int | None = Field(default=None, ge=1)


def _strip_trailing_semicolon(sql: str) -> str:
    return sql.strip().rstrip(";").strip()


def _validate(sql: str) -> str:
    body = _strip_trailing_semicolon(sql)
    if ";" in body:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            error_detail("multiple_statements", "Only one SQL statement per request.",
                         hint="Remove the ';' and send one statement. Use a CTE (WITH ...) if "
                              "you need several steps."),
        )
    if _WRITE_WORDS.match(body):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            error_detail("not_read_only", "Only read queries are allowed.",
                         hint="This API is read-only; the database role it uses has no write "
                              "permission. Start your statement with SELECT or WITH."),
        )
    if not _READ_START.match(body):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            error_detail("not_a_query", "Statement must begin with SELECT, WITH, TABLE or EXPLAIN.",
                         hint="See GET /api/ai/schema for the tables and columns available."),
        )
    return body


@router.post("/query")
async def run_query(
    body: QueryIn,
    request: Request,
    key: AuthedKey = Depends(authenticate),
) -> dict:
    """Run one read-only statement and return its rows.

    See `GET /api/ai/schema` for tables and columns, and mind the caveats it lists — most
    importantly that `calls` contains rows with a NULL `started_at` which are ingestion
    artifacts, not calls.
    """
    for scope in (SCOPE_SQL, SCOPE_CONTENT):
        if not key.has(scope):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                error_detail(
                    "missing_scope",
                    f"/api/ai/query requires both the 'sql' and 'content' scopes; this key is "
                    f"missing '{scope}'.",
                    hint="A role that can run SELECT can read transcripts, SMS bodies and "
                         "customer addresses, so ad-hoc SQL is treated as content access. Use "
                         "the curated /api/ai/* endpoints if the key should stay metrics-only.",
                ),
            )

    # Validate BEFORE checking configuration: "that is a write, and writes are never allowed"
    # is true on every deployment, so it is the more useful thing to say. Reporting
    # `sql_not_configured` for a DELETE would send the caller chasing the wrong problem.
    statement = _validate(body.sql)

    sessions = _ro_sessions()
    if sessions is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            error_detail(
                "sql_not_configured",
                "Ad-hoc SQL is not available on this deployment.",
                hint="POSTGRES_RO_USER / POSTGRES_RO_PASSWORD are unset. The read-only database "
                     "role is the security boundary for this endpoint, so it refuses to run "
                     "rather than fall back to the application's read/write credentials. "
                     "Use the curated /api/ai/* endpoints meanwhile.",
            ),
        )

    cap = min(body.limit or settings.AI_API_SQL_DEFAULT_LIMIT, settings.AI_API_SQL_MAX_LIMIT)

    started = time.monotonic()
    rows: list[dict] = []
    columns: list[str] = []
    truncated = False
    err: str | None = None
    code = 200
    try:
        async with sessions() as db:
            # READ ONLY is redundant given the role, and cheap; the timeout is not redundant.
            await db.execute(text("SET TRANSACTION READ ONLY"))
            await db.execute(
                text(f"SET LOCAL statement_timeout = {int(settings.AI_API_SQL_TIMEOUT_SECONDS) * 1000}")
            )
            # Wrapping is what makes the cap non-negotiable: a caller's own LIMIT still applies
            # inside, but cannot raise the ceiling.
            result = await db.execute(text(f"SELECT * FROM (\n{statement}\n) AS _q LIMIT {cap + 1}"))
            columns = list(result.keys())
            fetched = result.fetchall()
            truncated = len(fetched) > cap
            rows = [dict(zip(columns, r)) for r in fetched[:cap]]
            await db.rollback()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - report the DB's own message, it is the useful part
        err = str(exc).split("\n")[0][:1000]
        code = 400
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            error_detail("query_failed", f"The query failed: {err}",
                         hint="Check table and column names against GET /api/ai/schema. "
                              "Permission errors on `users`, `api_keys` or `api_key_usage` are "
                              "intentional and cannot be granted."),
        ) from exc
    finally:
        await record_usage(
            key.id, request.url.path, code, int((time.monotonic() - started) * 1000),
            rows=len(rows), sql=statement[:20000], err=err,
        )

    notes = [NOTE_PHANTOM]
    if truncated:
        notes.append(f"Results were truncated to {cap} rows. Pass a higher `limit` (max "
                     f"{settings.AI_API_SQL_MAX_LIMIT}) or aggregate in SQL instead of paging.")

    return ok(
        summary=f"{len(rows)} row(s) returned in {int((time.monotonic() - started) * 1000)}ms"
                + (f" (truncated at {cap})" if truncated else "") + ".",
        data={"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated},
        applied_filters={"sql": statement, "limit": cap,
                         "statement_timeout_seconds": settings.AI_API_SQL_TIMEOUT_SECONDS},
        notes=notes,
    )
