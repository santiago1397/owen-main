"""Authentication, scoping, rate limiting and audit for the AI API.

This is the ONLY way into `/api/ai/*`. Three things happen on every request:

1. **Authenticate** — the presented key is hashed and looked up by hash (O(1), indexed).
   Inactive, revoked and expired keys are rejected with the same message shape as an unknown
   key, so a probe cannot distinguish "wrong key" from "revoked key".
2. **Authorize** — the route declares the scope it needs; a key without it gets 403 naming the
   missing scope, because an AI that is told exactly what it lacks can report that to you
   instead of retrying blindly.
3. **Meter and record** — a per-key token bucket throttles, and an `api_key_usage` row is
   written best-effort after the response. Auditing must never fail a request, so every write
   here is wrapped and swallowed.

Errors are deliberately instructive rather than terse: a machine caller cannot ask a follow-up
question, so a 4xx body carries the valid values and a hint. See `error()` in envelope.py.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ai import periods
from app.api.ai.envelope import error_detail
from app.core.apikeys import SCOPES, extract_key, hash_key
from app.core.config import settings
from app.db import SessionLocal, get_db
from app.models import ApiKey, ApiKeyUsage

logger = logging.getLogger(__name__)


# --- rate limiting -------------------------------------------------------------------
# In-process sliding window per (key, bucket). The `app` container runs 4 uvicorn workers, so
# the effective limit is up to 4x the configured value — accepted deliberately: this exists to
# stop a runaway loop from saturating a 1-CPU container, not to meter billing. A Postgres-backed
# limiter would put a write on every read request to fix an approximation nobody needs exact.
_WINDOW_SECONDS = 60.0
_buckets: dict[tuple[str, str], deque[float]] = {}


def _check_rate(key_id: str, bucket: str, limit: int) -> float | None:
    """Record a hit; return seconds-to-wait if the caller is over `limit`, else None."""
    now = time.monotonic()
    hits = _buckets.setdefault((key_id, bucket), deque())
    cutoff = now - _WINDOW_SECONDS
    while hits and hits[0] < cutoff:
        hits.popleft()
    if len(hits) >= limit:
        return max(1.0, round(_WINDOW_SECONDS - (now - hits[0]), 1))
    hits.append(now)
    return None


# --- authentication ------------------------------------------------------------------
class AuthedKey:
    """The authenticated caller, attached to `request.state` so the audit layer can see it."""

    def __init__(self, row: ApiKey) -> None:
        self.id = str(row.id)
        self.name = row.name
        self.scopes: list[str] = list(row.scopes or [])

    def has(self, scope: str) -> bool:
        return scope in self.scopes


async def authenticate(
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_owen_key: str | None = Header(default=None, alias="X-OWEN-Key"),
) -> AuthedKey:
    if not settings.AI_API_ENABLED:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            error_detail("ai_api_disabled", "The AI API is disabled on this deployment.",
                         hint="Set AI_API_ENABLED=true and redeploy."),
        )

    presented = extract_key(authorization, x_owen_key)
    if not presented:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            error_detail(
                "missing_key",
                "No API key was presented.",
                hint="Send the key as `X-OWEN-Key: owen_sk_...` or "
                     "`Authorization: Bearer owen_sk_...`. Keys are issued in the OWEN UI "
                     "under API Keys.",
            ),
        )

    row = (
        await db.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(presented)))
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    # One message for every rejection reason: a caller probing keys learns only "not valid".
    if (
        row is None
        or not row.active
        or row.revoked_at is not None
        or (row.expires_at is not None and row.expires_at <= now)
    ):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            error_detail("invalid_key", "That API key is not valid, has been revoked, or has expired.",
                         hint="Issue a new key in the OWEN UI under API Keys."),
        )

    key = AuthedKey(row)
    request.state.ai_key = key

    # `last_used_at` answers "is this key dead or is someone still using it" at a glance.
    # Best-effort and non-blocking on failure: never fail a read because a stamp failed.
    try:
        await db.execute(update(ApiKey).where(ApiKey.id == row.id).values(last_used_at=now))
        await db.commit()
    except Exception:  # noqa: BLE001 - bookkeeping must not break the request
        await db.rollback()

    limit = (
        settings.AI_API_SQL_RATE_LIMIT_PER_MIN
        if request.url.path.rstrip("/").endswith("/query")
        else settings.AI_API_RATE_LIMIT_PER_MIN
    )
    bucket = "sql" if request.url.path.rstrip("/").endswith("/query") else "read"
    retry_after = _check_rate(key.id, bucket, limit)
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            error_detail(
                "rate_limited",
                f"Rate limit exceeded: {limit} requests/minute for this key on {bucket} endpoints.",
                hint=f"Wait {retry_after}s and retry. Prefer one wide query over many narrow ones "
                     f"— every endpoint accepts a date range and returns a full series.",
            ),
            headers={"Retry-After": str(int(retry_after))},
        )
    return key


def resolve_window(period, date_from, date_to):
    """Resolve a time window, turning a bad `period` into a 400 that lists the valid ones.

    Shared by every endpoint that takes a window. It lives here rather than in `periods.py`
    so that module stays free of HTTP concerns and unit-testable on its own — and it is
    shared rather than copied because the copy is exactly what went wrong: the content
    endpoints called `periods.resolve` directly and answered 500 on a typo'd period.
    """
    try:
        return periods.resolve(period, date_from, date_to)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            error_detail(
                "unknown_period",
                f"Unknown period {str(exc)!r}.",
                hint="Use one of the named periods, or pass explicit date_from/date_to.",
                valid_periods=periods.PERIODS,
            ),
        ) from exc


def require_scope(scope: str):
    """Dependency factory: gate a route on one scope."""

    async def _dep(key: AuthedKey = Depends(authenticate)) -> AuthedKey:
        if not key.has(scope):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                error_detail(
                    "missing_scope",
                    f"This key does not have the '{scope}' scope.",
                    hint=f"{SCOPES.get(scope, '')} Grant it in the OWEN UI under API Keys "
                         f"(this key currently has: {', '.join(key.scopes) or 'none'}).",
                ),
            )
        return key

    return _dep


# --- audit ---------------------------------------------------------------------------
async def usage_middleware(request: Request, call_next):
    """Record every `/api/ai/*` request, not just the interesting ones.

    Without this only `/query` was audited, because that route records itself. A key issued
    to an outside integration would then show "0 requests in 24h" in the UI no matter how
    hard it was being used — the audit trail would be quietly missing precisely the traffic
    it exists to show.

    `/query` still records itself (it alone knows the SQL and the row count) and flags the
    request so it is not counted twice.
    """
    # Both API-key surfaces, not just /api/ai. `/api/agent-runtime/*` (AI_AGENT_SPEC D13) is
    # the one that can MUTATE platform data, so leaving it out would mean the audit trail
    # covered every read and none of the writes — exactly backwards.
    if not request.url.path.startswith(("/api/ai", "/api/agent-runtime")):
        return await call_next(request)
    started = time.monotonic()
    response = await call_next(request)
    key = getattr(request.state, "ai_key", None)
    if key is not None and not getattr(request.state, "ai_usage_recorded", False):
        await record_usage(key.id, request.url.path, response.status_code,
                           int((time.monotonic() - started) * 1000))
    return response


async def record_usage(
    key_id: str | None,
    endpoint: str,
    status_code: int,
    duration_ms: int,
    rows: int | None = None,
    sql: str | None = None,
    err: str | None = None,
) -> None:
    """Write one `api_key_usage` row on its own session, swallowing every failure.

    Its own session because it runs after the request's session may already be closed, and
    swallowing because an audit failure that 500s the request would be worse than the gap it
    leaves in the log.
    """
    if key_id is None:
        return
    try:
        async with SessionLocal() as db:
            db.add(ApiKeyUsage(
                api_key_id=key_id, endpoint=endpoint, status_code=status_code,
                duration_ms=duration_ms, rows=rows, sql=sql, error=err,
            ))
            await db.commit()
    except Exception:  # noqa: BLE001 - auditing must never break a request
        logger.debug("api_key_usage write failed for %s", endpoint, exc_info=True)
