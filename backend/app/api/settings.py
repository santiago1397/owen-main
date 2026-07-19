"""Settings surface (Phase 5): masked provider/credential status + the webhook URLs to
paste into Twilio/SignalWire, plus the active analysis engines."""

from fastapi import APIRouter, Depends, Request

from app.analysis.classification import CATEGORIES
from app.api.deps import current_user
from app.core.config import settings
from app.models import User

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _mask(value: str) -> str:
    if not value:
        return ""
    return f"…{value[-4:]}" if len(value) > 4 else "set"


@router.get("")
async def get_settings(request: Request, _: User = Depends(current_user)) -> dict:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.hostname or "")
    base = f"{proto}://{host}"
    return {
        "business_tz": settings.BUSINESS_TZ,
        "categories": CATEGORIES,
        "providers": {
            "twilio": {
                "configured": bool(settings.TWILIO_AUTH_TOKEN),
                "account_sid": _mask(settings.TWILIO_ACCOUNT_SID),
                "status_webhook": f"{base}/webhooks/twilio/status",
                "recording_webhook": f"{base}/webhooks/twilio/recording",
                "message_webhook": f"{base}/webhooks/twilio/message",
            },
            "signalwire": {
                "configured": bool(settings.SIGNALWIRE_AUTH_TOKEN),
                "project_id": _mask(settings.SIGNALWIRE_PROJECT_ID),
                "space_url": settings.SIGNALWIRE_SPACE_URL,
                "status_webhook": f"{base}/webhooks/signalwire/status",
                "recording_webhook": f"{base}/webhooks/signalwire/recording",
                "message_webhook": f"{base}/webhooks/signalwire/message",
            },
        },
        "engines": {
            "transcription": settings.TRANSCRIPTION_ENGINE,
            "analysis": settings.ANALYSIS_ENGINE,
            "analysis_model": settings.ANALYSIS_MODEL,
        },
        "ghl": {
            "inbound_relay_configured": bool(settings.GHL_INBOUND_WEBHOOK_URL),
            "call_relay_configured": bool(settings.GHL_CALL_WEBHOOK_URL),
        },
    }
