"""GoHighLevel client — inbound relay only.

We POST to GHL Workflow "Inbound Webhook" trigger URLs: a plain JSON POST with no
auth/OAuth. GHL's own workflow decides what to do with the payload (create/update contact,
log to a conversation, notify). We never send outbound SMS from here, so this never incurs
SignalWire messaging cost.

Two independent relays, each with its own trigger URL so calls and texts can feed different
workflows: inbound SMS (post_inbound_message) and completed calls (post_call_summary).
"""

import httpx

from app.core.config import settings


async def _post(url: str, payload: dict) -> None:
    """POST payload to a GHL inbound-webhook URL. No-op when the URL is unset (relay
    disabled). Raises on non-2xx so the worker retries the job."""
    if not url:
        return
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url, json=payload, headers={"Content-Type": "application/json"}
        )
        resp.raise_for_status()


async def post_inbound_message(payload: dict) -> None:
    """POST a normalized inbound-message payload to the GHL inbound-SMS webhook."""
    await _post(settings.GHL_INBOUND_WEBHOOK_URL, payload)


async def post_call_summary(payload: dict) -> None:
    """POST a completed-call summary (attribution + AI analysis) to the GHL call webhook."""
    await _post(settings.GHL_CALL_WEBHOOK_URL, payload)
