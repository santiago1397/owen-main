import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db import get_db
from app.models import Caller, User
from app.schemas.api import CallerOut, CallerUpdate, Page

router = APIRouter(prefix="/api/callers", tags=["callers"])


@router.get("", response_model=Page)
async def list_callers(
    q: str | None = None,
    label: str | None = None,
    spam_score_gt: float | None = None,
    is_new: bool | None = None,  # global: called only once so far
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Page:
    stmt = select(Caller)
    if q:
        stmt = stmt.where(Caller.phone_number.ilike(f"%{q}%"))
    if label:
        stmt = stmt.where(Caller.label == label)
    if spam_score_gt is not None:
        stmt = stmt.where(Caller.spam_score > spam_score_gt)
    if is_new is True:
        stmt = stmt.where(Caller.total_calls <= 1)
    elif is_new is False:
        stmt = stmt.where(Caller.total_calls > 1)

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(Caller.last_seen_at.desc().nullslast()).offset((page - 1) * page_size).limit(page_size)
    callers = (await db.execute(stmt)).scalars().all()
    items = [
        CallerOut(
            id=c.id, phone_number=c.phone_number, first_seen_at=c.first_seen_at,
            last_seen_at=c.last_seen_at, total_calls=c.total_calls,
            spam_score=float(c.spam_score) if c.spam_score is not None else None, label=c.label,
        )
        for c in callers
    ]
    return Page(items=items, page=page, page_size=page_size, total=total)


@router.patch("/{caller_id}", response_model=CallerOut)
async def update_caller(
    caller_id: uuid.UUID,
    body: CallerUpdate,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> CallerOut:
    """Manual label override, e.g. mark a caller as 'known spam' (decision #5)."""
    caller = await db.get(Caller, caller_id)
    if caller is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "caller not found")
    caller.label = body.label
    await db.commit()
    return CallerOut(
        id=caller.id, phone_number=caller.phone_number, first_seen_at=caller.first_seen_at,
        last_seen_at=caller.last_seen_at, total_calls=caller.total_calls,
        spam_score=float(caller.spam_score) if caller.spam_score is not None else None,
        label=caller.label,
    )
