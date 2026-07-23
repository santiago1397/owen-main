"""AI voice-agent CRUD + versioning + activation (BulkVS+Asterisk platform, Ticket 11).

Additive and mirrors app/api/flows.py exactly: an append-only agent envelope (`agents`)
pointing at immutable config snapshots (`agent_versions`). Saving a version always INSERTs
(version = prior max + 1) and never mutates a prior row. Validation
(app.agents.validate_agent_config) GATES ACTIVATION only — drafts save freely; activation is
refused (HTTP 400) on hard errors and returns warnings.

Agents are a reusable LIBRARY: an agent is NEVER bound to a number, only referenced from a
flow's `ai_agent` node (which pins the specific agent_version at call time). This router does
not touch any existing call/analysis/flow path.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.service import next_version_number, validate_agent_config
from app.api.deps import current_user
from app.db import get_db
from app.models import Agent, AgentVersion, User
from app.schemas.api import (
    AgentActivationResult,
    AgentCreate,
    AgentDetail,
    AgentOut,
    AgentVersionOut,
    AgentVersionSave,
)

router = APIRouter(prefix="/api/agents", tags=["agents"])


def _agent_out(agent: Agent) -> AgentOut:
    return AgentOut(
        id=agent.id,
        name=agent.name,
        active_version_id=agent.active_version_id,
        created_at=agent.created_at,
    )


def _version_out(v: AgentVersion) -> AgentVersionOut:
    return AgentVersionOut(
        id=v.id, agent_id=v.agent_id, version=v.version, config=v.config, created_at=v.created_at
    )


@router.get("", response_model=list[AgentOut])
async def list_agents(
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AgentOut]:
    rows = (await db.execute(select(Agent).order_by(Agent.name))).scalars().all()
    return [_agent_out(a) for a in rows]


@router.post("", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: AgentCreate,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentOut:
    agent = Agent(name=body.name)
    db.add(agent)
    await db.commit()
    return _agent_out(agent)


@router.get("/{agent_id}", response_model=AgentDetail)
async def get_agent(
    agent_id: uuid.UUID,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentDetail:
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    versions = (
        await db.execute(
            select(AgentVersion).where(AgentVersion.agent_id == agent_id).order_by(AgentVersion.version)
        )
    ).scalars().all()
    return AgentDetail(
        id=agent.id,
        name=agent.name,
        active_version_id=agent.active_version_id,
        created_at=agent.created_at,
        versions=[_version_out(v) for v in versions],
    )


@router.get("/{agent_id}/versions", response_model=list[AgentVersionOut])
async def list_versions(
    agent_id: uuid.UUID,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AgentVersionOut]:
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    versions = (
        await db.execute(
            select(AgentVersion).where(AgentVersion.agent_id == agent_id).order_by(AgentVersion.version)
        )
    ).scalars().all()
    return [_version_out(v) for v in versions]


@router.post("/{agent_id}/versions", response_model=AgentVersionOut, status_code=status.HTTP_201_CREATED)
async def save_version(
    agent_id: uuid.UUID,
    body: AgentVersionSave,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentVersionOut:
    """Save a NEW immutable version of the agent's config. Never mutates a prior version: the
    new row's version is (current max + 1). Saving does not run validation — drafts may be
    incomplete; validation gates activation."""
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    existing = (
        await db.execute(select(AgentVersion.version).where(AgentVersion.agent_id == agent_id))
    ).scalars().all()
    version = AgentVersion(
        agent_id=agent_id, version=next_version_number(existing), config=body.config
    )
    db.add(version)
    await db.commit()
    return _version_out(version)


@router.post("/{agent_id}/versions/{version_id}/activate", response_model=AgentActivationResult)
async def activate_version(
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentActivationResult:
    """Validate then activate a version. Refuses (HTTP 400) if the config has hard errors;
    warnings never block. On success the agent's active pointer moves to this version."""
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    version = await db.get(AgentVersion, version_id)
    if version is None or version.agent_id != agent_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent version not found")

    errors, warnings = validate_agent_config(version.config or {})
    if errors:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"errors": errors, "warnings": warnings},
        )

    agent.active_version_id = version.id
    await db.commit()
    return AgentActivationResult(
        activated=True, version_id=version.id, errors=[], warnings=warnings
    )
