"""The CRM publishes a voice agent's version here (voice agents phase 2b, 2026-09-25).

The CRM is where the owner edits an agent's persona now; owen-main is where it runs. A CRM
Publish becomes ONE `agent_versions` row, appended exactly as `api/agents.py` saves one and
gated exactly as it activates one (`validate_agent_config`). Two routes use this module:

  * `POST /api/crm-link/agent-versions` — append (and activate) a published CRM version.
  * `GET  /api/crm-link/agent-versions` — every agent's ACTIVE config, so the CRM's
    `python -m app.ai.import_voice_agent` can copy the live agent instead of the owner
    retyping it. Read-only.

Rules, each one a test in `tests/test_crm_agent_versions.py`:

  * **The agent is found by NAME, and never created.** An unknown name is 404; two agents
    with one name is 409 naming both ids — this module never guesses which one the CRM meant.
  * **Validation is activation's.** Hard errors refuse (422) with every problem; nothing is
    written. Warnings are returned, never block.
  * **Idempotent on the CRM version.** The stored config carries `crm_version` (and
    `crm_agent_id` when sent). A second push of the same CRM version answers the version that
    already exists and appends nothing — a retried push after a lost response must not leave
    two rows. The same CRM version with DIFFERENT content is refused (409): CRM versions are
    immutable, so that is a second CRM database or a bug, and silently choosing either copy
    would be wrong.

Pure kernel (`plan`) + thin async glue, so the decision is testable with no database.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.service import next_version_number, validate_agent_config

# The keys this module adds to a stored config. Everything else is the CRM's to send.
CRM_VERSION_KEY = "crm_version"
CRM_AGENT_KEY = "crm_agent_id"


def stamped(config: dict, crm_version: int, crm_agent_id: int | None) -> dict:
    """The config as it will be stored: the CRM's, plus which CRM version it is."""
    out = dict(config)
    out[CRM_VERSION_KEY] = crm_version
    if crm_agent_id is not None:
        out[CRM_AGENT_KEY] = crm_agent_id
    return out


def _same_origin(stored: dict, crm_version: int, crm_agent_id: int | None) -> bool:
    if not isinstance(stored, dict) or stored.get(CRM_VERSION_KEY) != crm_version:
        return False
    # A version pushed without an agent id matches any agent id, and vice versa: the CRM
    # started sending it with this module, so there is no older row that lacks it by design.
    theirs = stored.get(CRM_AGENT_KEY)
    return theirs is None or crm_agent_id is None or theirs == crm_agent_id


@dataclass
class Plan:
    """What `publish` should do. `action` is one of:

      "refuse"    — validation failed; `errors` says why. Write nothing.
      "conflict"  — this CRM version exists with different content. Write nothing.
      "existing"  — this CRM version is already stored as `existing_id`. Append nothing;
                    move the active pointer only if `activate` and it is not already there.
      "append"    — insert version `version` with `config`, then activate if asked.
    """

    action: str
    config: dict = field(default_factory=dict)
    version: int | None = None
    existing_id: object = None
    existing_version: int | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def plan(existing: list[tuple[object, int, dict]], config: dict, crm_version: int,
         crm_agent_id: int | None = None) -> Plan:
    """Decide, from the agent's existing versions as `(id, version, config)`.

    Idempotency is checked BEFORE validation: a version already stored was valid when it was
    stored, and answering "already there" for a retry must not depend on today's rules."""
    want = stamped(config, crm_version, crm_agent_id)
    for vid, number, stored in existing:
        if _same_origin(stored, crm_version, crm_agent_id):
            if _comparable(stored) != _comparable(want):
                return Plan("conflict", existing_id=vid, existing_version=number,
                            errors=[f"CRM version {crm_version} is already version {number} "
                                    "here with different content"])
            return Plan("existing", config=stored, existing_id=vid, existing_version=number)
    errors, warnings = validate_agent_config(want)
    if errors:
        return Plan("refuse", errors=errors, warnings=warnings)
    return Plan("append", config=want,
                version=next_version_number([n for _, n, _ in existing]),
                warnings=warnings)


def _comparable(cfg: dict) -> dict:
    # The agent id is allowed to be absent on one side (see _same_origin).
    return {k: v for k, v in (cfg or {}).items() if k != CRM_AGENT_KEY}


def refusal_message(errors: list[str]) -> str:
    """One sentence the CRM can show as-is (its client reads `detail.message`)."""
    if len(errors) == 1:
        return f"the phone system refused this version: {errors[0]}"
    return "the phone system refused this version: " + "; ".join(errors)


# --- the async glue -------------------------------------------------------------------------

async def agents_named(db, name: str) -> list:
    from sqlalchemy import select

    from app.models import Agent

    return list((await db.execute(select(Agent).where(Agent.name == name))).scalars().all())


async def versions_of(db, agent_id) -> list[tuple[object, int, dict]]:
    from sqlalchemy import select

    from app.models import AgentVersion

    rows = (await db.execute(
        select(AgentVersion).where(AgentVersion.agent_id == agent_id)
        .order_by(AgentVersion.version)
    )).scalars().all()
    return [(v.id, v.version, v.config or {}) for v in rows]


async def active_agents(db) -> list[dict]:
    """Every agent, with its ACTIVE version's config (None when nothing is active)."""
    from sqlalchemy import select

    from app.models import Agent, AgentVersion

    agents = (await db.execute(select(Agent).order_by(Agent.name))).scalars().all()
    out = []
    for a in agents:
        active = await db.get(AgentVersion, a.active_version_id) if a.active_version_id \
            else None
        out.append({
            "agent_id": str(a.id),
            "name": a.name,
            "active_version": None if active is None else {
                "id": str(active.id),
                "version": active.version,
                "config": active.config or {},
                "created_at": active.created_at.isoformat() if active.created_at else None,
            },
        })
    return out


class Refused(Exception):
    """A publish that will not be written. `status` is the HTTP answer, `errors` the why."""

    def __init__(self, status: int, message: str, errors: list[str] | None = None,
                 warnings: list[str] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.errors = errors or []
        self.warnings = warnings or []


async def publish(db, *, agent_name: str, config: dict, crm_version: int,
                  crm_agent_id: int | None = None, activate: bool = True) -> dict:
    """Append (or recognise) one CRM version. Raises `Refused`; commits on success."""
    from sqlalchemy.exc import IntegrityError

    from app.models import AgentVersion

    name = (agent_name or "").strip()
    found = await agents_named(db, name)
    if not found:
        raise Refused(404, f"the phone system has no agent named '{name}'")
    if len(found) > 1:
        ids = ", ".join(str(a.id) for a in found)
        raise Refused(409, f"the phone system has {len(found)} agents named '{name}' "
                           f"({ids}); rename one so the CRM can tell them apart")
    agent = found[0]

    # Two tries: a concurrent push of the next version can take the number we planned
    # (uq_agent_version). The second plan sees that row — and if it was THIS crm_version,
    # answers "existing" rather than appending a duplicate.
    for attempt in (1, 2):
        p = plan(await versions_of(db, agent.id), config, crm_version, crm_agent_id)
        if p.action == "refuse":
            raise Refused(422, refusal_message(p.errors), p.errors, p.warnings)
        if p.action == "conflict":
            raise Refused(409, p.errors[0], p.errors)
        created = p.action == "append"
        if created:
            row = AgentVersion(agent_id=agent.id, version=p.version, config=p.config)
            db.add(row)
            try:
                await db.flush()
            except IntegrityError:
                await db.rollback()
                if attempt == 2:
                    raise
                continue
            version_id, version = row.id, p.version
        else:
            version_id, version = p.existing_id, p.existing_version
        if activate:
            agent.active_version_id = version_id
        await db.commit()
        return {
            "ok": True,
            "agent_id": str(agent.id),
            "version_id": str(version_id),
            "version": version,
            "crm_version": crm_version,
            "created": created,
            "active": agent.active_version_id == version_id,
            "warnings": p.warnings,
        }
    raise AssertionError("unreachable")
