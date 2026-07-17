"""Shared webhook router builder. Both providers use the identical flow — verify
signature, persist the event, enqueue slow work, return 200 fast — differing only in
the adapter, provider name, and which header carries the signature.
"""

import base64
import hmac
import logging

from fastapi import APIRouter, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.core.config import settings
from app.db import SessionLocal
from app.models import Call, Number
from app.providers.base import ProviderAdapter
from app.services import queue
from app.services.ingestion import ingest_status_event
from app.services.messages import ingest_message_event
from app.services.recordings import ingest_recording_event

logger = logging.getLogger("webhooks")


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


def _cfb_basic_auth_ok(request: Request) -> bool:
    """Call Flow Builder's generic 'Request' node can't produce a Twilio-style HMAC
    signature, so it authenticates via HTTP Basic Auth (embedded in its URL field)
    against a shared secret instead. Only meaningful for signalwire."""
    secret = settings.SIGNALWIRE_CFB_WEBHOOK_SECRET
    auth = request.headers.get("authorization", "")
    if not (secret and auth.startswith("Basic ")):
        return False
    try:
        _, _, password = base64.b64decode(auth[6:]).decode().partition(":")
    except Exception:
        return False
    return hmac.compare_digest(password, secret)


async def _fallback_call_sid(db: AsyncSession, tracking_number: str) -> str | None:
    """Correlate a recording to a call by tracking number + recency, for when Call Flow
    Builder's CallSid-equivalent template variable doesn't resolve to a usable value."""
    number = (
        await db.execute(select(Number).where(Number.phone_number == tracking_number))
    ).scalar_one_or_none()
    if number is None:
        return None
    call = (
        await db.execute(
            select(Call)
            .where(Call.number_id == number.id)
            .order_by(Call.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return call.provider_call_sid if call else None


def build_router(adapter: ProviderAdapter, provider: str, signature_headers: list[str]) -> APIRouter:
    router = APIRouter(prefix=f"/webhooks/{provider}", tags=["webhooks"])

    async def _verified(request: Request) -> dict[str, str] | None:
        # TEMP DIAGNOSTIC (remove after SMS webhook is confirmed): dump exactly what the
        # provider sent so we can see headers/signature/body shape. request.body() is
        # cached by Starlette, so the later form()/json() parse still works.
        _raw = await request.body()
        logger.info("%s webhook DIAG: method=%s ct=%r headers=%s raw[:800]=%r",
                    provider, request.method,
                    request.headers.get("content-type", ""),
                    {k: v for k, v in request.headers.items()
                     if k.lower() in ("content-type", "content-length", "user-agent",
                                      "x-signalwire-signature", "x-twilio-signature",
                                      "authorization", "signature", "x-forwarded-proto",
                                      "x-forwarded-host")},
                    _raw[:800])
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body = await request.json()
            params = {k: str(v) for k, v in body.items()} if isinstance(body, dict) else {}
        else:
            form = await request.form()
            params = {k: str(v) for k, v in form.items()}
        # Call Flow Builder's own event schema doesn't reliably identify which tracking
        # number was originally dialed (see providers/signalwire.py), so we pass it
        # explicitly as a query param on the webhook URL instead of trusting the payload.
        tracking_number = request.query_params.get("tracking_number")
        if tracking_number:
            params["_tracking_number"] = tracking_number
        sig = _signature(request, signature_headers)
        logger.info("%s webhook hit: path=%s content_type=%s sig_present=%s keys=%s",
                    provider, request.url.path, content_type, bool(sig), sorted(params.keys()))

        if provider == "signalwire" and _cfb_basic_auth_ok(request):
            logger.info("%s webhook: verified via Call Flow Builder basic-auth secret", provider)
            return params

        if not adapter.verify_signature(_public_url(request), params, sig):
            logger.warning("%s webhook: signature verification FAILED (url=%s)",
                           provider, _public_url(request))
            return None
        return params

    @router.post("/status")
    async def status(request: Request) -> Response:
        params = await _verified(request)
        if params is None:
            return Response(status_code=403)
        evt = adapter.parse_status_event(params)
        logger.info("%s status: call_sid=%s status=%s from=%s to=%s direction=%s",
                    provider, evt.provider_call_sid, evt.status, evt.from_number,
                    evt.to_number, evt.direction)
        async with SessionLocal() as db:  # type: AsyncSession
            await ingest_status_event(db, provider, evt)
        return Response(status_code=200)

    @router.post("/recording")
    async def recording(request: Request) -> Response:
        params = await _verified(request)
        if params is None:
            return Response(status_code=403)
        rec = adapter.parse_recording_event(params)
        logger.info("%s recording: call_sid=%s recording_sid=%s status=%s url=%s",
                    provider, rec.provider_call_sid, rec.provider_recording_sid,
                    rec.status, rec.provider_url)
        async with SessionLocal() as db:
            tracking_number = params.get("_tracking_number")
            if (not rec.provider_call_sid or "%{" in rec.provider_call_sid) and tracking_number:
                fallback_sid = await _fallback_call_sid(db, tracking_number)
                if fallback_sid:
                    logger.info(
                        "%s recording: CallSid unresolved (%r), falling back to most "
                        "recent call %s for tracking_number=%s",
                        provider, rec.provider_call_sid, fallback_sid, tracking_number,
                    )
                    rec.provider_call_sid = fallback_sid
            recording_row = await ingest_recording_event(db, provider, rec)
            if (rec.status or "").lower() == "completed":
                logger.info("%s recording: enqueueing recording_fetch for recording_id=%s",
                            provider, recording_row.id)
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
            else:
                logger.info("%s recording: status=%s not completed yet, not enqueueing",
                            provider, rec.status)
        return Response(status_code=200)

    @router.post("/message")
    async def message(request: Request) -> Response:
        params = await _verified(request)
        if params is None:
            return Response(status_code=403)
        evt = adapter.parse_message_event(params)
        logger.info("%s message: sid=%s from=%s to=%s num_media=%s",
                    provider, evt.provider_message_sid, evt.from_number,
                    evt.to_number, evt.num_media)
        async with SessionLocal() as db:  # type: AsyncSession
            msg = await ingest_message_event(db, provider, evt)
            await queue.enqueue(db, "message_relay_ghl", {"message_id": str(msg.id)})
        return Response(status_code=200)

    return router
