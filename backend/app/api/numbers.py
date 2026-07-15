from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db import get_db
from app.models import Call, Campaign, Number, Provider, User
from app.schemas.api import NumberStats

router = APIRouter(prefix="/api/numbers", tags=["numbers"])


@router.get("", response_model=list[NumberStats])
async def list_numbers(
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[NumberStats]:
    rows = (
        await db.execute(
            select(
                Number.id,
                Number.phone_number,
                Number.friendly_name,
                Number.forwards_to,
                Number.active,
                Provider.name.label("provider"),
                Campaign.name.label("campaign_name"),
                func.count(Call.id).label("total_calls"),
                func.max(Call.started_at).label("last_call_at"),
            )
            .join(Provider, Number.provider_id == Provider.id, isouter=True)
            .join(Campaign, Number.campaign_id == Campaign.id, isouter=True)
            .join(Call, Call.number_id == Number.id, isouter=True)
            .group_by(Number.id, Provider.name, Campaign.name)
            .order_by(func.count(Call.id).desc())
        )
    ).mappings().all()
    return [NumberStats(**dict(r)) for r in rows]
