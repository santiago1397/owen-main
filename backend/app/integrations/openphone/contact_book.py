"""The name Quo's own contact book gives a phone number — read, cached, never written.

The CRM shows it on a thread for a number that is NOT a CRM contact, labelled "from Quo",
and creates nothing from it (the owner's rule: an unknown number is never saved as a
contact). So OWEN only has to answer "does Quo know this number, and as whom?".

`GET /contacts` is the only way to ask — there is no lookup-by-number (spec D11a) — so the
whole book is paged once and kept for `TTL_SECONDS`. The poll already pages the same book
every tick to find participants, and hands its pages to `remember()`, so on a running
mirror the cache is normally warm and a delivery costs no request at all.

Read-only, like everything that touches `openphone_client`: this module calls
`list_contacts` and nothing else. No name is ever logged.
"""

from __future__ import annotations

import time
from typing import Iterable, Optional

from app.integrations.openphone import config as op_config

TTL_SECONDS = 600
MAX_PAGES = 100

_names: dict[str, str] = {}
_loaded_at: float = 0.0


def display_name(entry: dict) -> str:
    """"First Last", else the company, else "" — from OpenPhone's verified shape."""
    fields = entry.get("defaultFields") if isinstance(entry.get("defaultFields"), dict) else {}
    name = " ".join(str(fields.get(k) or "").strip()
                    for k in ("firstName", "lastName")).strip()
    return name or str(fields.get("company") or "").strip()


def remember(entries: Iterable[dict]) -> None:
    """Record the names on a page of `GET /contacts`. Called by the poll as it pages."""
    from app.integrations.openphone.sync import _numbers_from_contact

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = display_name(entry)
        if not name:
            continue
        for number in _numbers_from_contact(entry):
            key = op_config.match_key(number)
            if len(key) == 10:
                _names[key] = name[:200]


async def _reload(page_limit: int = 50) -> None:
    global _loaded_at
    from app.integrations.openphone.sync import _next_token, _page_items
    from app.providers import openphone_client as op

    token: Optional[str] = None
    for _ in range(MAX_PAGES):
        body = await op.list_contacts(page_token=token, limit=page_limit)
        remember(_page_items(body))
        token = _next_token(body)
        if not token:
            break
    _loaded_at = time.monotonic()


async def name_for(number: str | None) -> str:
    """Quo's name for `number`, or "". Never raises: a name is decoration."""
    key = op_config.match_key(number)
    if len(key) != 10:
        return ""
    if key not in _names and time.monotonic() - _loaded_at > TTL_SECONDS:
        try:
            await _reload()
        except Exception:  # noqa: BLE001 - no name is a fine answer
            return ""
    return _names.get(key, "")


def forget() -> None:
    """Empty the cache (tests)."""
    global _loaded_at
    _names.clear()
    _loaded_at = 0.0
