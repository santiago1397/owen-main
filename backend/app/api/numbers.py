from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db import get_db
from app.models import Call, Campaign, Number, Provider, User
from app.schemas.api import NumberStats
from app.services.number_sync import derive_lifecycle

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
                # BulkVS+Asterisk split identity + soft-release marker (Ticket 03).
                Number.owner_provider,
                Number.media_provider,
                Number.released_at,
                Number.provider_status,
                # Selected only to DERIVE lifecycle below; popped before building the schema.
                Number.campaign_id,
                Number.flow_id,
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

    out: list[NumberStats] = []
    for r in rows:
        d = dict(r)
        d["lifecycle"] = derive_lifecycle(
            active=d["active"],
            released_at=d["released_at"],
            campaign_id=d.pop("campaign_id"),
            flow_id=d.pop("flow_id"),
            provider_status=d["provider_status"],
        )
        out.append(NumberStats(**d))
    return out
