from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db import get_db
from app.models import Campaign, User
from app.schemas.api import CampaignOut

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


@router.get("", response_model=list[CampaignOut])
async def list_campaigns(
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CampaignOut]:
    rows = (
        await db.execute(select(Campaign).order_by(Campaign.name))
    ).scalars().all()
    return [CampaignOut(id=c.id, name=c.name, active=c.active) for c in rows]
