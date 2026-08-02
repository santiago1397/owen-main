"""API-key management (JWT-authed — this is the UI's surface, not the AI's).

Deliberately NOT reachable with an API key: a machine credential must never be able to mint
or widen another machine credential. Issuing, listing, revoking and reading usage all require
a logged-in user, exactly like the rest of `/api/*`.

The plaintext key is returned by POST and nowhere else, ever. There is no "reveal" endpoint
because there is no stored plaintext to reveal.
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.apikeys import (
    DEFAULT_SCOPES,
    SCOPES,
    display_prefix,
    generate_key,
    hash_key,
    normalize_scopes,
)
from app.db import get_db
from app.models import ApiKey, ApiKeyUsage, User

router = APIRouter(prefix="/api/api-keys", tags=["api-keys"])


class CreateKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_SCOPES))
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


def _out(row: ApiKey, requests_24h: int = 0) -> dict:
    now = datetime.now(timezone.utc)
    expired = row.expires_at is not None and row.expires_at <= now
    return {
        "id": str(row.id),
        "name": row.name,
        "key_prefix": row.key_prefix,
        "scopes": list(row.scopes or []),
        "active": bool(row.active) and row.revoked_at is None and not expired,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "expired": expired,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "requests_24h": requests_24h,
    }


@router.get("")
async def list_keys(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(current_user),
) -> dict:
    rows = (await db.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))).scalars().all()
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    counts = dict(
        (await db.execute(
            select(ApiKeyUsage.api_key_id, func.count())
            .where(ApiKeyUsage.at >= since)
            .group_by(ApiKeyUsage.api_key_id)
        )).all()
    )
    return {
        "items": [_out(r, counts.get(r.id, 0)) for r in rows],
        "scopes": SCOPES,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_key(
    body: CreateKeyIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Mint a key. The `key` field in the response is the ONLY time the secret exists
    outside the caller's hands — it is hashed before storage and cannot be recovered."""
    unknown = [s for s in body.scopes if s.strip().lower() not in SCOPES]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unknown scope(s): {', '.join(unknown)}. Valid scopes: {', '.join(SCOPES)}",
        )
    scopes = normalize_scopes(body.scopes)
    if not scopes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "at least one scope is required")

    plaintext = generate_key()
    row = ApiKey(
        name=body.name.strip(),
        key_prefix=display_prefix(plaintext),
        key_hash=hash_key(plaintext),
        scopes=scopes,
        created_by_user_id=user.id,
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
            if body.expires_in_days else None
        ),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {**_out(row), "key": plaintext}


@router.delete("/{key_id}")
async def revoke_key(
    key_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(current_user),
) -> dict:
    """Revoke, not delete: the key row is kept so its usage history stays attributable
    (the audit rows cascade on delete, and losing them would be the wrong trade)."""
    row = await db.get(ApiKey, key_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "api key not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        row.active = False
        await db.commit()
    return {"status": "revoked", "id": str(key_id)}


@router.get("/{key_id}/usage")
async def key_usage(
    key_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(current_user),
) -> dict:
    """Recent requests made with this key — the answer to "what is that integration doing".
    Includes the SQL text for /query calls."""
    if await db.get(ApiKey, key_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "api key not found")
    rows = (
        await db.execute(
            select(ApiKeyUsage).where(ApiKeyUsage.api_key_id == key_id)
            .order_by(ApiKeyUsage.at.desc()).limit(limit)
        )
    ).scalars().all()
    return {
        "items": [
            {
                "at": r.at.isoformat() if r.at else None,
                "endpoint": r.endpoint,
                "status_code": r.status_code,
                "duration_ms": r.duration_ms,
                "rows": r.rows,
                "sql": r.sql,
                "error": r.error,
            }
            for r in rows
        ]
    }
