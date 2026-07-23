"""Call-flow CRUD + versioning + activation (BulkVS+Asterisk platform, Ticket 02).

Additive: this router is purely for managing flow graphs; it does not touch any existing
Twilio/SignalWire/GHL/recording/analysis path. A later ticket builds the ARI interpreter
that EXECUTES an activated version; another builds the operator UI.

Append-only versioning: saving a version always INSERTs (version = prior max + 1) and
never mutates a prior row. Validation (app.flows.validator) GATES ACTIVATION only —
drafts save freely; activation is refused (HTTP 400) on hard errors and returns warnings.
"""

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db import get_db
from app.flows import next_version_number, validate_graph
from app.models import Flow, FlowVersion, User
from app.schemas.api import (
    ActivationResult,
    FlowCreate,
    FlowDetail,
    FlowOut,
    FlowVersionOut,
    FlowVersionSave,
)

logger = logging.getLogger("api.flows")

router = APIRouter(prefix="/api/flows", tags=["flows"])


def _schedule_tts_prewarm(graph: dict) -> None:
    """Fire-and-forget TTS synthesis of every static prompt in an activated graph (Ticket
    15.2). Strictly best-effort: scheduling or synthesis failures are logged and NEVER
    block or fail activation — call-time lazy synthesis is the backstop."""
    try:
        from app.services.tts import prewarm_graph_prompts

        asyncio.create_task(prewarm_graph_prompts(graph))
    except Exception:  # noqa: BLE001 - prewarm must never affect activation
        logger.exception("flow activation: could not schedule TTS prompt prewarm")


def _flow_out(flow: Flow) -> FlowOut:
    return FlowOut(
        id=flow.id,
        name=flow.name,
        active_version_id=flow.active_version_id,
        created_at=flow.created_at,
    )


def _version_out(v: FlowVersion) -> FlowVersionOut:
    return FlowVersionOut(
        id=v.id, flow_id=v.flow_id, version=v.version, graph=v.graph, created_at=v.created_at
    )


@router.get("", response_model=list[FlowOut])
async def list_flows(
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[FlowOut]:
    rows = (await db.execute(select(Flow).order_by(Flow.name))).scalars().all()
    return [_flow_out(f) for f in rows]


@router.post("", response_model=FlowOut, status_code=status.HTTP_201_CREATED)
async def create_flow(
    body: FlowCreate,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> FlowOut:
    flow = Flow(name=body.name)
    db.add(flow)
    await db.commit()
    return _flow_out(flow)


@router.get("/{flow_id}", response_model=FlowDetail)
async def get_flow(
    flow_id: uuid.UUID,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> FlowDetail:
    flow = await db.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "flow not found")
    versions = (
        await db.execute(
            select(FlowVersion).where(FlowVersion.flow_id == flow_id).order_by(FlowVersion.version)
        )
    ).scalars().all()
    return FlowDetail(
        id=flow.id,
        name=flow.name,
        active_version_id=flow.active_version_id,
        created_at=flow.created_at,
        versions=[_version_out(v) for v in versions],
    )


@router.get("/{flow_id}/versions", response_model=list[FlowVersionOut])
async def list_versions(
    flow_id: uuid.UUID,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[FlowVersionOut]:
    flow = await db.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "flow not found")
    versions = (
        await db.execute(
            select(FlowVersion).where(FlowVersion.flow_id == flow_id).order_by(FlowVersion.version)
        )
    ).scalars().all()
    return [_version_out(v) for v in versions]


@router.post("/{flow_id}/versions", response_model=FlowVersionOut, status_code=status.HTTP_201_CREATED)
async def save_version(
    flow_id: uuid.UUID,
    body: FlowVersionSave,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> FlowVersionOut:
    """Save a NEW immutable version of the flow's graph. Never mutates a prior version:
    the new row's version is (current max + 1). Saving does not run validation — drafts
    may be structurally incomplete; validation gates activation."""
    flow = await db.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "flow not found")
    existing = (
        await db.execute(select(FlowVersion.version).where(FlowVersion.flow_id == flow_id))
    ).scalars().all()
    version = FlowVersion(
        flow_id=flow_id, version=next_version_number(existing), graph=body.graph
    )
    db.add(version)
    await db.commit()
    return _version_out(version)


@router.post("/{flow_id}/versions/{version_id}/activate", response_model=ActivationResult)
async def activate_version(
    flow_id: uuid.UUID,
    version_id: uuid.UUID,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> ActivationResult:
    """Validate then activate a version. Refuses (HTTP 400) if the graph has hard errors;
    warnings never block. On success the flow's active pointer is moved to this version."""
    flow = await db.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "flow not found")
    version = await db.get(FlowVersion, version_id)
    if version is None or version.flow_id != flow_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "flow version not found")

    result = validate_graph(version.graph or {})
    if not result.ok:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"errors": result.errors, "warnings": result.warnings},
        )

    flow.active_version_id = version.id
    await db.commit()
    # Ticket 15.2: pre-synthesize TTS for every static prompt so the first live call plays
    # from cache. Best-effort background task — never blocks or fails the activation.
    _schedule_tts_prewarm(version.graph or {})
    return ActivationResult(
        activated=True, version_id=version.id, errors=[], warnings=result.warnings
    )
