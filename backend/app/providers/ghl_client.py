"""GoHighLevel client — inbound SMS relay only.

We POST inbound texts to a GHL Workflow "Inbound Webhook" trigger URL: a plain JSON POST
with no auth/OAuth. GHL's own workflow decides what to do with the payload (create/update
contact, log to a conversation, notify). We never send outbound SMS from here, so this
never incurs SignalWire messaging cost.
"""

import httpx

from app.core.config import settings


def _configured() -> bool:
    return bool(settings.GHL_INBOUND_WEBHOOK_URL)


async def post_inbound_message(payload: dict) -> None:
    """POST a normalized inbound-message payload to the GHL inbound webhook.

    No-op when unconfigured. Raises on non-2xx so the worker retries the job.
    """
    if not _configured():
        return
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            settings.GHL_INBOUND_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
