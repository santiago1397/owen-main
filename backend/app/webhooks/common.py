"""Shared webhook router builder. Both providers use the identical flow — verify
signature, persist the event, enqueue slow work, return 200 fast — differing only in
the adapter, provider name, and which header carries the signature.
"""

from fastapi import APIRouter, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.providers.base import ProviderAdapter
from app.services import queue
from app.services.ingestion import ingest_status_event
from app.services.recordings import ingest_recording_event


def _public_url(request: Request) -> str:
    # Behind Traefik, reconstruct the external https URL the provider actually signed.
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.hostname or "")
    return f"{proto}://{host}{request.url.path}"


def _signature(request: Request, headers: list[str]) -> str:
    for h in headers:
        val = request.headers.get(h)
        if val:
            return val
    return ""


def build_router(adapter: ProviderAdapter, provider: str, signature_headers: list[str]) -> APIRouter:
    router = APIRouter(prefix=f"/webhooks/{provider}", tags=["webhooks"])

    async def _verified(request: Request) -> dict[str, str] | None:
        form = await request.form()
        params = {k: str(v) for k, v in form.items()}
        sig = _signature(request, signature_headers)
        if not adapter.verify_signature(_public_url(request), params, sig):
            return None
        return params

    @router.post("/status")
    async def status(request: Request) -> Response:
        params = await _verified(request)
        if params is None:
            return Response(status_code=403)
        async with SessionLocal() as db:  # type: AsyncSession
            await ingest_status_event(db, provider, adapter.parse_status_event(params))
        return Response(status_code=200)

    @router.post("/recording")
    async def recording(request: Request) -> Response:
        params = await _verified(request)
        if params is None:
            return Response(status_code=403)
        rec = adapter.parse_recording_event(params)
        async with SessionLocal() as db:
            recording_row = await ingest_recording_event(db, provider, rec)
            if (rec.status or "").lower() == "completed":
                await queue.enqueue(
                    db,
                    "recording_fetch",
                    {
                        "provider": provider,
                        "recording_id": str(recording_row.id),
                        "recording_sid": rec.provider_recording_sid,
                        "provider_url": rec.provider_url,
                    },
                )
        return Response(status_code=200)

    return router
