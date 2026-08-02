"""API-key minting, hashing and scope vocabulary for the AI API (`/api/ai/*`).

Separate from `core/security.py` on purpose: that file is user auth (argon2 + JWT) and webhook
signatures. This is the third access-control surface — machine credentials — and it has
different rules:

- The secret is 32 bytes of `secrets.token_bytes`, so there is nothing to brute-force. We hash
  with plain SHA-256 rather than argon2 because verification happens on EVERY request; argon2
  would add ~100ms of deliberate work to the hot path and buy no security against a
  256-bit-entropy secret.
- The plaintext is returned exactly once, at creation, and never persisted. There is no
  "show key" endpoint and no recovery — losing it means issuing a new one.
- Lookup is by hash (indexed), not by scanning and comparing, so auth is O(1).

Scopes are capabilities, not roles, and are all READ-only in v1. Nothing here can mutate
platform data; key management itself is JWT-authed in `api/api_keys.py`.
"""

from __future__ import annotations

import hashlib
import secrets

# The four capabilities. They are separate because they leak different things: `read` exposes
# counts, `content` exposes what customers said and where they live, `sql` exposes anything the
# read-only DB role can see, and `logs` exposes error text that often embeds phone numbers.
SCOPE_READ = "read"
SCOPE_CONTENT = "content"
SCOPE_SQL = "sql"
SCOPE_LOGS = "logs"

SCOPES: dict[str, str] = {
    SCOPE_READ: "Curated metrics: call/lead/message counts, durations, series, pipeline health.",
    SCOPE_CONTENT: "Call transcripts, AI summaries, SMS bodies, and customer PII from parsed emails.",
    SCOPE_SQL: "Run read-only SQL via /api/ai/query.",
    SCOPE_LOGS: "Read captured warnings/errors, failed jobs and failed relays via /api/ai/errors.",
}

# Every key issued through the UI/CLI without an explicit scope list gets this.
DEFAULT_SCOPES = [SCOPE_READ]

KEY_PREFIX = "owen_sk_"
# Enough of the plaintext to identify a key in a list without being useful to an attacker.
DISPLAY_PREFIX_CHARS = len(KEY_PREFIX) + 6


def generate_key() -> str:
    """Mint a new plaintext key: `owen_sk_<43 url-safe chars>` (32 bytes of entropy)."""
    return f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_key(plaintext: str) -> str:
    """SHA-256 hex of the plaintext. Stored; the plaintext is not."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def display_prefix(plaintext: str) -> str:
    """The leading fragment stored in the clear so the UI can label a key."""
    return plaintext[:DISPLAY_PREFIX_CHARS]


def normalize_scopes(scopes: list[str] | None) -> list[str]:
    """Keep only known scopes, de-duplicated and in a stable order.

    Unknown scopes are dropped rather than rejected here so that a scope removed in a future
    version can't strand an existing key; the CRUD layer validates user input up front and
    tells the caller which scopes are valid.
    """
    if not scopes:
        return list(DEFAULT_SCOPES)
    seen = {s.strip().lower() for s in scopes if s and s.strip().lower() in SCOPES}
    return [s for s in SCOPES if s in seen]


def extract_key(authorization: str | None, x_owen_key: str | None) -> str | None:
    """Pull the presented key out of either accepted header.

    `X-OWEN-Key: owen_sk_...` is the documented form; `Authorization: Bearer owen_sk_...` is
    accepted too because most HTTP clients and integration platforms default to Bearer. The
    `owen_sk_` prefix is what distinguishes an API key from a user JWT on the same header.
    """
    if x_owen_key and x_owen_key.strip():
        return x_owen_key.strip()
    if authorization:
        value = authorization.strip()
        if value.lower().startswith("bearer "):
            value = value[7:].strip()
        if value.startswith(KEY_PREFIX):
            return value
    return None
