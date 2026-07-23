import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.config import settings
from app.db import get_db
from app.flows.service import flow_assignment_error
from app.models import Call, Campaign, Flow, Number, Provider, User
from app.schemas.api import NumberFlowAssign, NumberFlowOut, NumberStats
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


@router.patch("/{number_id}", response_model=NumberFlowOut)
async def assign_flow(
    number_id: uuid.UUID,
    body: NumberFlowAssign,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> NumberFlowOut:
    """Assign a flow to a number (Ticket 15.5), or unassign with `{"flow_id": null}`.

    Guards (the pure kernel is app.flows.service.flow_assignment_error):
    - 400 unless the number's media rides on the Asterisk platform
      (`media_provider == settings.BULKVS_MEDIA_PROVIDER`) — the runtime resolves flows by
      (phone_number, media_provider), so a flow anywhere else could never execute;
    - 400 unless the flow exists AND has an active version (activation is the go-live gate).
    Unassignment is always allowed."""
    number = await db.get(Number, number_id)
    if number is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "number not found")

    if body.flow_id is not None:
        flow = await db.get(Flow, body.flow_id)
        error = flow_assignment_error(
            number_media_provider=number.media_provider,
            expected_media_provider=settings.BULKVS_MEDIA_PROVIDER,
            flow_exists=flow is not None,
            flow_active_version_id=flow.active_version_id if flow is not None else None,
        )
        if error is not None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, error)

    number.flow_id = body.flow_id
    await db.commit()
    return NumberFlowOut(id=number.id, phone_number=number.phone_number, flow_id=number.flow_id)
