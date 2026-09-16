"""The one PUBLIC route this integration has: the picture BulkVS fetches (2026-09-16).

    GET /crm-media/{media_id}?e=<expiry>&t=<signature>

No API key, no session, no CORS — a carrier's MMSC cannot present any of those. What
authorises the request is the signature, which only OWEN can produce; the design, the
exposure it creates and why it is the minimum are written out in `media.py`.

**Its own router, deliberately.** `/api/crm-link/*` is API-key gated and fenced at an exact
route list by a test; a public route must not live inside that surface, where a future
reader could mistake it for one more gated endpoint. It also mounts at `/crm-media` rather
than under `/api`, so it cannot be caught by a rule written for the API.

Every failure is the SAME 404: a bad signature, an expired one, an id that never existed,
an id that has been swept, and the feature being switched off. One answer, so the route
cannot be used to learn which.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response

from app.integrations.crm import config as crm_config
from app.integrations.crm import media as crm_media

logger = logging.getLogger("integrations.crm.public_media")

router = APIRouter(prefix="/crm-media", tags=["crm-media"])


@router.get("/{media_id}")
async def fetch_media(media_id: str, e: str | None = None, t: str | None = None) -> Response:
    """Serve one outbound picture to the carrier.

    Read-only by construction: GET is the only method on the router, there is no id the
    caller chooses, and the response is bytes that were sniffed as an image before they
    were ever stored.
    """
    if not crm_config.link_enabled():
        return Response(status_code=404)
    cfg = crm_media.current()
    if not crm_media.verify(media_id, e, t, cfg):
        # Not logged with the id: a log line per probe on a public route is a log-flooding
        # surface, and the id is the secret.
        logger.info("crm-media: refused a fetch — bad or expired link")
        return Response(status_code=404)
    found = crm_media.read(media_id, cfg)
    if found is None:
        return Response(status_code=404)
    data, content_type = found
    return Response(
        content=data,
        media_type=content_type,
        headers={
            # `no-store` because this is one customer's photograph on a public URL: no
            # shared cache anywhere on the path may keep it after the link has expired.
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            # Nothing on this server links to it and nothing should index it.
            "X-Robots-Tag": "noindex, nofollow",
        },
    )
