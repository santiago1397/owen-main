"""`/api/retell/*` — the two PUBLIC doors Retell uses (decision 20).

    POST /api/retell/webhook             call_started | call_ended | call_analyzed
    POST /api/retell/functions/{name}    transfer | end_call | capture_lead | request_change

Not behind OWEN's login and not behind an API key — Retell has neither. Instead EVERY request
must carry Retell's signature over its raw body (signature.py), checked before the body is
parsed. The order of the guards, and what each does:

  1. no RETELL_API_KEY            -> 503 "Retell is not configured". Nothing read.
  2. bad / stale / missing signature -> 401 with the reason. Nothing parsed, nothing done,
     and the log line names the reason only — never the key, never the body.
  3. a body that is not a JSON object -> 400. Nothing done.
  4. a function name OWEN does not have, or a body naming a different function -> 404 / 400.
  5. otherwise the webhook (webhook.py) or the function (functions.py) runs.

The key is the HMAC key — Retell signs with the account's API key — which is why it can only
ever live in owen-main's environment (decision 20).
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.integrations.retell import functions as retell_functions
from app.integrations.retell import registry
from app.integrations.retell import signature
from app.integrations.retell import webhook as retell_webhook

logger = logging.getLogger("integrations.retell.api")

router = APIRouter(prefix="/api/retell", tags=["retell"])

REFUSE_NOT_CONFIGURED = "Retell is not configured on this phone system (RETELL_API_KEY is unset)"


async def _verified_body(request: Request, what: str) -> dict:
    key = (settings.RETELL_API_KEY or "").strip()
    if not key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, REFUSE_NOT_CONFIGURED)
    raw = await request.body()
    refusal = signature.verify(raw, request.headers.get(signature.HEADER), key)
    if refusal:
        logger.warning("retell: REFUSED %s — %s", what, refusal)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, refusal)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the body is not JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the body is not a JSON object")
    return body


@router.post("/webhook")
async def retell_webhook_route(request: Request):
    """Retell's call lifecycle webhook. 200 for anything handled, ignored or already seen
    (Retell retries a non-2xx); 500 only when processing failed and a retry should redo it."""
    body = await _verified_body(request, "webhook")
    code, answer = await retell_webhook.handle(body, registry.current())
    return JSONResponse(answer, status_code=code)


@router.post("/functions/{name}")
async def retell_function_route(name: str, request: Request) -> dict:
    """One custom function. The URL names it; a body naming another is refused, because
    the two disagreeing means a misconfigured dashboard and guessing is the wrong answer."""
    if name not in retell_functions.FUNCTIONS:
        # Checked after the signature: an unsigned caller learns nothing about which exist.
        await _verified_body(request, f"function {name}")
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no Retell function named {name!r}")
    body = await _verified_body(request, f"function {name}")
    named = str(body.get("name") or name)
    if named != name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"this URL is the {name} function; the body names {named!r}")
    return await retell_functions.handle(name, body, registry.current())
