"""SignalWire Compatibility (LaML) REST client for reconciliation. Same Calls resource
shape as Twilio, served from the project's space URL."""

from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import settings
from app.providers.base import NormalizedCallEvent
from app.providers.cxml import normalize_call


async def fetch_recent_calls(window_hours: int) -> list[NormalizedCallEvent]:
    if not (settings.SIGNALWIRE_PROJECT_ID and settings.SIGNALWIRE_AUTH_TOKEN and settings.SIGNALWIRE_SPACE_URL):
        return []
    since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).strftime("%Y-%m-%d")
    base = f"https://{settings.SIGNALWIRE_SPACE_URL}/api/laml/2010-04-01"
    url = f"{base}/Accounts/{settings.SIGNALWIRE_PROJECT_ID}/Calls.json"
    auth = (settings.SIGNALWIRE_PROJECT_ID, settings.SIGNALWIRE_AUTH_TOKEN)
    events: list[NormalizedCallEvent] = []
    async with httpx.AsyncClient(timeout=30) as client:
        params = {"StartTime>": since, "PageSize": "1000"}
        next_url: str | None = url
        while next_url:
            resp = await client.get(next_url, params=params if next_url == url else None, auth=auth)
            resp.raise_for_status()
            data = resp.json()
            events.extend(normalize_call(c) for c in data.get("calls", []))
            nxt = data.get("next_page_uri")
            next_url = f"https://{settings.SIGNALWIRE_SPACE_URL}{nxt}" if nxt else None
    return events
