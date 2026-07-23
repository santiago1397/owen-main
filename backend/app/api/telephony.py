"""Operator softphone endpoints (Ticket 13) — WebRTC calling control plane.

Two authenticated surfaces, both gated on ASTERISK_ENABLED (503 when the platform is dark):

1. POST /api/telephony/webrtc/credentials — minted at app-login time. Returns short-lived SIP
   (per-operator pjsip WebRTC endpoint) + ephemeral coturn TURN creds. The REAL gate is app
   login (current_user); the browser NEVER talks to ARI.

2. ARI CONTROL endpoints (hold / unhold / bridge / blind-transfer). SIP.js drives only its own
   leg; ALL bridge/hold/transfer go through the BACKEND over ARI here — never browser->ARI.
   Thin wrappers over the pure orchestration in app/telephony/control.py, run against the
   server-side AsteriskAriClient.

The ARI client is resolved via `get_ari_control()` so tests can substitute a FAKE.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.config import settings
from app.db import get_db
from app.models import Number, User
from app.telephony import control, outbound
from app.telephony.credentials import build_webrtc_credentials

router = APIRouter(prefix="/api/telephony", tags=["telephony"])


def get_ari_control():
    """The server-side ARI control client. Indirection so tests inject a FAKE and no live
    Asterisk/httpx is needed. Imported lazily so importing this module doesn't require httpx."""
    from app.providers.asterisk_client import AsteriskAriClient

    return AsteriskAriClient()


def _require_enabled() -> None:
    if not settings.ASTERISK_ENABLED:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "telephony platform disabled")


# --- 1. credential minting (app-login time) ------------------------------------------------

@router.post("/webrtc/credentials")
async def webrtc_credentials(user: User = Depends(current_user)) -> dict:
    """Mint short-lived SIP + TURN creds for THIS operator's browser softphone. Authenticated
    (current_user) — the app-login gate is the real boundary. Operator identity = user.email."""
    _require_enabled()
    return build_webrtc_credentials(
        operator_id=user.email,
        sip_secret=settings.OPERATOR_SIP_SECRET,
        sip_domain=settings.OPERATOR_SIP_DOMAIN,
        wss_url=settings.OPERATOR_WSS_URL,
        turn_secret=settings.TURN_STATIC_SECRET,
        turn_urls=settings.turn_urls,
        sip_ttl_seconds=settings.OPERATOR_SIP_TTL_SECONDS,
        turn_ttl_seconds=settings.TURN_TTL_SECONDS,
    )


# --- 2. ARI control (server-side only) -----------------------------------------------------

class HoldIn(BaseModel):
    channel_id: str
    hold: bool = True


class BridgeIn(BaseModel):
    channel_a: str  # e.g. the operator browser leg
    channel_b: str  # e.g. the caller channel (same Linkedid)


class TransferIn(BaseModel):
    channel_id: str
    kind: str       # "did" | "operator" | "ai_agent"
    target: str     # number / operator id / agent id


@router.post("/control/hold")
async def control_hold(body: HoldIn, user: User = Depends(current_user)) -> dict:
    """Hold / unhold a channel (backend-driven; SIP.js never does this itself)."""
    _require_enabled()
    ari = get_ari_control()
    if body.hold:
        await control.hold(ari, body.channel_id)
    else:
        await control.unhold(ari, body.channel_id)
    return {"ok": True, "held": body.hold}


@router.post("/control/bridge")
async def control_bridge(body: BridgeIn, user: User = Depends(current_user)) -> dict:
    """Bridge the operator's browser leg with the caller channel (both under one Linkedid)."""
    _require_enabled()
    ari = get_ari_control()
    bridge_id = await control.bridge(ari, body.channel_a, body.channel_b)
    if not bridge_id:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "could not create bridge")
    return {"ok": True, "bridge_id": bridge_id}


@router.post("/control/transfer")
async def control_transfer(body: TransferIn, user: User = Depends(current_user)) -> dict:
    """Blind-transfer a channel to a DID / another operator / the AI-agent runtime (v1)."""
    _require_enabled()
    if body.kind not in control.TRANSFER_KINDS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"bad transfer kind: {body.kind}")
    ari = get_ari_control()
    endpoint = await control.blind_transfer(
        ari, body.channel_id, body.kind, body.target, trunk_name=settings.BULKVS_TRUNK_NAME
    )
    if endpoint is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unresolvable transfer target")
    return {"ok": True, "transferred_to": endpoint}


# --- 3. Manual operator OUTBOUND calling (Ticket 14) ---------------------------------------

async def _owned_from_numbers(db: AsyncSession) -> list[Number]:
    """Owned BulkVS DIDs usable as the caller-ID for an outbound call: active, not soft-
    released, owner=bulkvs. Foreign/spoofed numbers are out of scope (locked design). Candidates
    are fetched by owner then filtered with the PURE predicate `outbound.is_owned_bulkvs_did`,
    so the restriction has one (unit-tested) source of truth."""
    rows = (
        await db.execute(
            select(Number)
            .where(Number.owner_provider == settings.BULKVS_OWNER_PROVIDER)
            .order_by(Number.phone_number)
        )
    ).scalars().all()
    return [
        n for n in rows
        if outbound.is_owned_bulkvs_did(n, owner_provider=settings.BULKVS_OWNER_PROVIDER)
    ]


@router.get("/outbound/from-numbers")
async def outbound_from_numbers(
    _: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> list[dict]:
    """The owned BulkVS DIDs the operator may place a call FROM (the from-number picker).
    The remembered default is a lightweight client-side preference (localStorage) — no schema."""
    _require_enabled()
    return [
        {"id": str(n.id), "phone_number": n.phone_number, "friendly_name": n.friendly_name}
        for n in await _owned_from_numbers(db)
    ]


class OutboundCallIn(BaseModel):
    callee_number: str          # who to dial (external, E.164)
    from_number: str            # owned BulkVS DID to present as caller-ID
    operator_channel_id: str | None = None  # if the operator's live Stasis leg is already known


async def _guardrail_warnings(db: AsyncSession, callee_number: str) -> list[str]:
    """Soft, NON-BLOCKING warnings: outside 8am–9pm (callee tz), and — only if the Ticket-10
    `sms_opt_outs` table exists — an opt-out hit. Missing table => opt-out check skipped."""
    warnings: list[str] = []
    tw = outbound.time_window_warning(callee_number, datetime.now(timezone.utc))
    if tw:
        warnings.append(tw)

    model = outbound.resolve_opt_out_model()
    if model is not None:
        try:
            row = (
                await db.execute(select(model).where(model.phone_number == callee_number).limit(1))
            ).first()
            ow = outbound.opt_out_warning(row is not None)
            if ow:
                warnings.append(ow)
        except Exception:  # noqa: BLE001 - table not migrated yet => skip silently (defensive)
            pass
    return warnings


@router.post("/outbound/call")
async def outbound_call(
    body: OutboundCallIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Place a manual operator outbound call: validate the from-number is an owned BulkVS DID,
    compute soft guardrail warnings, then orchestrate originate + pre-bridge consent + bridge
    over ARI (recording on by default). Guardrails are advisory — the call still proceeds."""
    _require_enabled()

    owned = {n.phone_number for n in await _owned_from_numbers(db)}
    if body.from_number not in owned:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "from_number must be an owned BulkVS DID",
        )
    if not body.callee_number.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "callee_number required")

    warnings = await _guardrail_warnings(db, body.callee_number)

    ari = get_ari_control()
    result = await control.place_outbound_call(
        ari,
        operator_id=user.email,
        callee_number=body.callee_number,
        from_number=body.from_number,
        trunk_name=settings.BULKVS_TRUNK_NAME,
        consent_media=settings.OUTBOUND_CONSENT_MEDIA or None,
        record=settings.OUTBOUND_RECORDING_ENABLED,
        operator_channel_id=body.operator_channel_id,
    )
    if not result.get("ok"):
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"could not place call: {result.get('reason', 'unknown')}",
        )
    return {**result, "warnings": warnings}
