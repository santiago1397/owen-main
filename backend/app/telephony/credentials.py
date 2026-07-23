"""Short-lived SIP + TURN credential minting for the operator WebRTC softphone (Ticket 13).

PURE (stdlib only) so it is unit-testable without fastapi/pydantic. The FastAPI endpoint in
app/api/telephony.py reads config + the authenticated user and calls `build_webrtc_credentials`.

Security model (locked design):
- The REAL gate is APP LOGIN: only an authenticated operator can reach the minting endpoint,
  and ARI stays server-side (the browser NEVER talks to ARI). SIP.js drives ONLY its own leg.
- SIP: a static per-operator `chan_pjsip` WebRTC endpoint lives in asterisk/pjsip.conf; the
  digest password is a per-deployment WebRTC secret rendered from env (${OPERATOR_SIP_SECRET}).
  We return it only to a logged-in operator, stamped with a short `expires_at` the frontend
  uses to re-mint (re-login/refresh) before it lapses. (A per-SESSION unique SIP password would
  require realtime PJSIP — out of scope here; the app-login gate + Traefik-fronted wss is the
  boundary, documented in asterisk/README.md.)
- TURN: genuinely ephemeral, using coturn's `use-auth-secret` (TURN REST API) scheme —
  username = "<unix-expiry>:<operator-slug>", credential = base64(HMAC-SHA1(secret, username)).
  coturn validates the HMAC and the expiry itself, so these creds are self-expiring with no
  server-side state. See asterisk/turnserver.conf.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from typing import Optional

# SIP usernames / coturn usernames must be URI-safe. Collapse anything else to '-'.
_SAFE = re.compile(r"[^a-zA-Z0-9_.-]+")


def operator_slug(operator_id: str) -> str:
    """A stable, SIP/URI-safe token identifying an operator (used as the PJSIP endpoint name
    suffix and inside the TURN username). Derived from the operator's identity (e.g. email),
    lower-cased, non-safe chars collapsed to '-'. Deterministic so the same operator always
    maps to the same endpoint `operator-<slug>` configured in pjsip.conf."""
    slug = _SAFE.sub("-", str(operator_id).strip().lower()).strip("-")
    return slug or "unknown"


def operator_endpoint_name(operator_id: str) -> str:
    """The PJSIP endpoint/aor/auth name for an operator's browser softphone (matches the
    `operator-<slug>` sections rendered into asterisk/pjsip.conf)."""
    return f"operator-{operator_slug(operator_id)}"


def operator_dial_endpoint(operator_id: str) -> str:
    """The ARI/dialplan technology+resource string to originate a call TO an operator's
    browser leg: `PJSIP/operator-<slug>`."""
    return f"PJSIP/{operator_endpoint_name(operator_id)}"


def mint_turn_credentials(
    secret: str, operator_id: str, ttl_seconds: int, *, now: Optional[float] = None
) -> dict:
    """Ephemeral coturn credentials (TURN REST API / `use-auth-secret`).

    Returns {"username", "credential", "ttl", "expires_at"}. `username` embeds the unix
    expiry so coturn can reject stale creds without any shared state; `credential` is
    base64(HMAC-SHA1(secret, username)). Empty secret -> empty creds (TURN disabled)."""
    ts = int(now if now is not None else time.time())
    expiry = ts + int(ttl_seconds)
    username = f"{expiry}:{operator_slug(operator_id)}"
    if not secret:
        return {"username": "", "credential": "", "ttl": int(ttl_seconds), "expires_at": expiry}
    mac = hmac.new(secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha1).digest()
    credential = base64.b64encode(mac).decode("ascii")
    return {
        "username": username,
        "credential": credential,
        "ttl": int(ttl_seconds),
        "expires_at": expiry,
    }


def build_webrtc_credentials(
    *,
    operator_id: str,
    sip_secret: str,
    sip_domain: str,
    wss_url: str,
    turn_secret: str,
    turn_urls: list[str],
    sip_ttl_seconds: int,
    turn_ttl_seconds: int,
    now: Optional[float] = None,
) -> dict:
    """Assemble the full credential blob the softphone registers with. PURE: every input is
    passed in (no config import), so it is exercised directly in tests.

    Shape (consumed by frontend/src/lib/softphone.ts):
      {
        "sip": {"endpoint", "username", "authorization_username", "password",
                "domain", "wss_url", "expires_at"},
        "ice_servers": [{"urls": [...], "username", "credential"}]  # STUN/TURN for SIP.js
      }
    """
    ts = int(now if now is not None else time.time())
    endpoint = operator_endpoint_name(operator_id)
    turn = mint_turn_credentials(turn_secret, operator_id, turn_ttl_seconds, now=ts)
    ice_servers = []
    if turn_urls:
        ice_servers.append(
            {
                "urls": list(turn_urls),
                "username": turn["username"],
                "credential": turn["credential"],
            }
        )
    return {
        "sip": {
            "endpoint": endpoint,
            # SIP.js registers as the endpoint; auth username == endpoint (pjsip auth section).
            "username": endpoint,
            "authorization_username": endpoint,
            "password": sip_secret,
            "domain": sip_domain,
            "wss_url": wss_url,
            "expires_at": ts + int(sip_ttl_seconds),
        },
        "ice_servers": ice_servers,
    }
