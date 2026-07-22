"""Non-gating telephony health surface (BulkVS + Asterisk platform, ticket 01).

Reports Asterisk/BulkVS reachability WITHOUT the container healthcheck depending on it —
that stays on /health (Postgres). When ASTERISK_ENABLED is off this returns a clean
'disabled' snapshot instead of erroring, and every ARI probe is best-effort, so the
endpoint never fails the request. Public + unauthenticated on purpose: it exposes only
booleans (no secrets), mirroring the plain /health probe.
"""

from fastapi import APIRouter

from app.core.config import settings
from app.providers import asterisk_client

router = APIRouter(tags=["health"])


@router.get("/health/telephony")
async def telephony_health() -> dict:
    if not settings.ASTERISK_ENABLED:
        # Flag off: telephony is dark by design. Report cleanly, probe nothing.
        return {"asterisk_enabled": False, "ari_reachable": False, "trunk_registered": False}
    ari_ok = await asterisk_client.ari_reachable()
    # Only meaningful to ask about the trunk once ARI answered.
    trunk_ok = await asterisk_client.trunk_registered() if ari_ok else False
    return {"asterisk_enabled": True, "ari_reachable": ari_ok, "trunk_registered": trunk_ok}
