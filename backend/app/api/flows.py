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
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db import get_db
from app.flows import next_version_number, validate_graph
from app.flows.service import flow_delete_plan, pick_clone_source
from app.models import Call, Flow, FlowVersion, Number, User
from app.schemas.api import (
    ActivationResult,
    FlowClone,
    FlowCreate,
    FlowDeleteResult,
    FlowDetail,
    FlowOut,
    FlowRename,
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


def _flow_out(flow: Flow, *, versions: int = 0, calls: int = 0) -> FlowOut:
    return FlowOut(
        id=flow.id,
        name=flow.name,
        active_version_id=flow.active_version_id,
        created_at=flow.created_at,
        archived_at=flow.archived_at,
        version_count=versions,
        attributed_call_count=calls,
    )


async def _flow_out_counted(db: AsyncSession, flow: Flow) -> FlowOut:
    """`_flow_out` with its counts actually filled in. Single-flow endpoints must use this:
    defaulting the counts to 0 would report "no versions" for a flow that has them, which a
    caller would reasonably believe (the UI decides whether Clone is even offered from it)."""
    versions = int(
        (
            await db.execute(
                select(func.count())
                .select_from(FlowVersion)
                .where(FlowVersion.flow_id == flow.id)
            )
        ).scalar_one()
        or 0
    )
    return _flow_out(flow, versions=versions, calls=await _attributed_call_count(db, flow.id))


async def _source_version(db: AsyncSession, flow: Flow) -> FlowVersion | None:
    """The version a clone copies. Loads this flow's (id, version) pairs and lets the pure
    `pick_clone_source` kernel decide (active, else latest); None if it has no version."""
    pairs = list(
        (
            await db.execute(
                select(FlowVersion.id, FlowVersion.version).where(
                    FlowVersion.flow_id == flow.id
                )
            )
        ).all()
    )
    chosen = pick_clone_source(active_version_id=flow.active_version_id, versions=pairs)
    return await db.get(FlowVersion, chosen) if chosen is not None else None


async def _attributed_call_count(db: AsyncSession, flow_id: uuid.UUID) -> int:
    """How many calls are pinned to ANY version of this flow. This is what makes a flow
    undeletable: calls.flow_version_id is call attribution, and the FK is NO ACTION."""
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(Call)
                .where(
                    Call.flow_version_id.in_(
                        select(FlowVersion.id).where(FlowVersion.flow_id == flow_id)
                    )
                )
            )
        ).scalar_one()
        or 0
    )


async def _assigned_numbers(db: AsyncSession, flow_id: uuid.UUID) -> list[str]:
    return list(
        (
            await db.execute(select(Number.phone_number).where(Number.flow_id == flow_id))
        ).scalars().all()
    )


def _version_out(v: FlowVersion) -> FlowVersionOut:
    return FlowVersionOut(
        id=v.id, flow_id=v.flow_id, version=v.version, graph=v.graph, created_at=v.created_at
    )


@router.get("", response_model=list[FlowOut])
async def list_flows(
    archived: bool = False,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[FlowOut]:
    """The flow library. Archived flows are HIDDEN by default (`?archived=1` includes them),
    so soft-deleting actually removes a flow from view. Each row carries its version count
    and attributed-call count so the UI can offer only actions that will succeed and can say
    up front whether a delete will destroy or archive."""
    stmt = select(Flow).order_by(Flow.name)
    if not archived:
        stmt = stmt.where(Flow.archived_at.is_(None))
    rows = (await db.execute(stmt)).scalars().all()

    # Two grouped counts for the whole page rather than 2N per-flow queries.
    version_counts = dict(
        (
            await db.execute(
                select(FlowVersion.flow_id, func.count()).group_by(FlowVersion.flow_id)
            )
        ).all()
    )
    call_counts = dict(
        (
            await db.execute(
                select(FlowVersion.flow_id, func.count(Call.id))
                .join(Call, Call.flow_version_id == FlowVersion.id)
                .group_by(FlowVersion.flow_id)
            )
        ).all()
    )
    return [
        _flow_out(
            f,
            versions=int(version_counts.get(f.id, 0)),
            calls=int(call_counts.get(f.id, 0)),
        )
        for f in rows
    ]


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


@router.post("/{flow_id}/clone", response_model=FlowOut, status_code=status.HTTP_201_CREATED)
async def clone_flow(
    flow_id: uuid.UUID,
    body: FlowClone,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> FlowOut:
    """Copy a flow's graph into a BRAND NEW flow, so a new flow can start from a working one
    instead of an empty canvas.

    WHAT IS COPIED: the graph of the source's ACTIVE version (what actually answers calls
    today), or its latest saved version if it was never activated. Verbatim — node ids are
    graph-scoped and no graph references its own flow/version id, so no rewriting is needed.

    WHAT IS NOT, and why:
      - number assignments: `numbers.flow_id` is a single FK, so a clone could only inherit
        one by STEALING it from the original. The copy answers nothing until assigned.
      - version history: a clone starts at v1. The source's history belongs to the source.
      - `ai_agent` node targets: `Agent` is explicitly a REUSABLE library object referenced
        by flows, never owned by one, so the copy points at the SAME agent. Same for the
        operator lists on dial nodes.
      - TTS prewarm: the clone is a draft nobody can call yet, and its prompts are the first
        thing you will rewrite — synthesizing them now would burn real API calls for audio
        that is about to be replaced. Activation prewarms, as always.

    The copy arrives as a DRAFT (active_version_id NULL): activation stays a deliberate,
    validated act, which also means cloning itself can never fail validation. Both INSERTs
    are one transaction — there is no DELETE-a-flow surface for a half-made clone to be
    cleaned up with, so a partial clone would be permanent litter.

    Name is required and NOT checked for uniqueness — matching `create_flow`, which has
    never enforced it (`flows.name` has no unique constraint)."""
    flow = await db.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "flow not found")
    name = body.name.strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "name is required")

    source = await _source_version(db, flow)
    if source is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "this flow has no saved version to clone",
        )

    clone = Flow(name=name)
    db.add(clone)
    await db.flush()  # assign clone.id without committing — one transaction for both rows
    db.add(FlowVersion(flow_id=clone.id, version=1, graph=source.graph))
    await db.commit()
    logger.info(
        "flow.clone source=%s source_version=%s -> flow=%s name=%r",
        flow_id, source.id, clone.id, name,
    )
    return _flow_out(clone, versions=1, calls=0)


@router.patch("/{flow_id}", response_model=FlowOut)
async def rename_flow(
    flow_id: uuid.UUID,
    body: FlowRename,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> FlowOut:
    """Rename a flow. Only the envelope's label changes — versions, the active pointer and
    every call already attributed to this flow are untouched."""
    flow = await db.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "flow not found")
    name = body.name.strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "name is required")
    flow.name = name
    await db.commit()
    return await _flow_out_counted(db, flow)


@router.delete("/{flow_id}", response_model=FlowDeleteResult)
async def delete_flow(
    flow_id: uuid.UUID,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> FlowDeleteResult:
    """Remove a flow from the library — really deleting it only when that destroys nothing.

    `calls.flow_version_id` pins each call to the flow version that handled it, and every FK
    in the chain is NO ACTION. So once a flow has taken a single call it cannot be deleted
    without either violating that constraint or erasing call attribution. Hence:
      - no call references any of its versions -> HARD DELETE (rows really gone);
      - otherwise -> archive (`archived_at`), hiding it while every reference stays valid.
    The response says which happened; the UI reads `attributed_call_count` from the list to
    warn the operator up front, so a permanent delete is never a surprise.

    Refused (409) while any number is still assigned. A hard delete would violate
    `fk_numbers_flow_id` anyway, and archiving an assigned flow would leave it hidden but
    STILL ANSWERING CALLS. Unassigning here instead would silently repoint a live DID at
    default handling, which is not something a delete button should do quietly."""
    flow = await db.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "flow not found")

    outcome, message = flow_delete_plan(
        assigned_numbers=await _assigned_numbers(db, flow_id),
        attributed_call_count=await _attributed_call_count(db, flow_id),
    )
    if outcome == "refused":
        raise HTTPException(status.HTTP_409_CONFLICT, message)

    if outcome == "archived":
        flow.archived_at = datetime.now(timezone.utc)
        await db.commit()
        logger.info("flow.archive flow=%s name=%r", flow_id, flow.name)
        return FlowDeleteResult(outcome="archived", flow_id=flow_id)

    # Hard delete. ORDER MATTERS: flows.active_version_id -> flow_versions.id and
    # flow_versions.flow_id -> flows.id form a CIRCULAR FK, so the pointer must be dropped
    # before the versions it points at, and the versions before the flow itself.
    await db.execute(update(Flow).where(Flow.id == flow_id).values(active_version_id=None))
    await db.execute(sa_delete(FlowVersion).where(FlowVersion.flow_id == flow_id))
    await db.execute(sa_delete(Flow).where(Flow.id == flow_id))
    await db.commit()
    logger.info("flow.delete flow=%s name=%r (no attributed calls)", flow_id, flow.name)
    return FlowDeleteResult(outcome="deleted", flow_id=flow_id)


@router.post("/{flow_id}/restore", response_model=FlowOut)
async def restore_flow(
    flow_id: uuid.UUID,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> FlowOut:
    """Un-archive a flow, returning it to the library. Without this, archiving would just be
    a slower delete."""
    flow = await db.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "flow not found")
    flow.archived_at = None
    await db.commit()
    return await _flow_out_counted(db, flow)


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
