"""GoHighLevel v2 (LeadConnector) API client — direct push of parsed job emails.

Auth is a sub-account Private Integration Token (PIT). We upsert a Contact and create an
Opportunity in a pipeline, so a Dispatch job becomes a card on the GHL job board without
using GHL's premium Inbound-Webhook trigger (no per-execution charge).

Every call is thin over httpx; response parsing is defensive (GHL nests the resource under
its type, e.g. {"contact": {...}}, but we tolerate a flat body too). Raises on non-2xx so
the relay job records the error and retries with backoff.
"""

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger("ghl_api")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.GHL_API_TOKEN}",
        "Version": settings.GHL_API_VERSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _extract_id(body: dict, *keys: str) -> str | None:
    """Pull an id out of a GHL response that may nest under the resource name."""
    for k in keys:
        node = body.get(k)
        if isinstance(node, dict) and node.get("id"):
            return node["id"]
    return body.get("id")


async def upsert_contact(payload: dict) -> dict:
    """POST /contacts/upsert — create or update by the location's duplicate rules. Returns
    the parsed response; contact id is at ['contact']['id']."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.GHL_API_BASE}/contacts/upsert", json=payload, headers=_headers()
        )
        resp.raise_for_status()
        return resp.json()


async def create_opportunity(payload: dict) -> dict:
    """POST /opportunities/ — create a deal in a pipeline. Opp id at ['opportunity']['id']."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.GHL_API_BASE}/opportunities/", json=payload, headers=_headers()
        )
        resp.raise_for_status()
        return resp.json()


async def add_contact_note(contact_id: str, body: str) -> None:
    """POST /contacts/{id}/notes — best-effort; caller should not fail the relay if this does."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.GHL_API_BASE}/contacts/{contact_id}/notes",
            json={"body": body}, headers=_headers(),
        )
        resp.raise_for_status()


async def list_pipelines() -> list[dict]:
    """GET /opportunities/pipelines?locationId= — used once to resolve pipeline/stage IDs."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{settings.GHL_API_BASE}/opportunities/pipelines",
            params={"locationId": settings.GHL_LOCATION_ID}, headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json().get("pipelines", [])


async def resolve_stage_id() -> tuple[str, str]:
    """Return (pipeline_id, stage_id). Uses configured IDs; if the stage is blank, resolves
    to the first stage of the configured pipeline. Raises if the pipeline can't be found."""
    pipeline_id = settings.GHL_PIPELINE_ID
    stage_id = settings.GHL_PIPELINE_STAGE_ID
    if pipeline_id and stage_id:
        return pipeline_id, stage_id
    pipelines = await list_pipelines()
    pl = None
    if pipeline_id:
        pl = next((p for p in pipelines if p.get("id") == pipeline_id), None)
    if pl is None and pipelines:
        pl = pipelines[0]
    if pl is None:
        raise RuntimeError("GHL: no pipelines found for this location")
    stages = pl.get("stages") or []
    if not stages:
        raise RuntimeError(f"GHL: pipeline {pl.get('name')} has no stages")
    return pl["id"], (stage_id or stages[0]["id"])
