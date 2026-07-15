"""Auth (JWT + argon2 passwords) and webhook signature verification.

Two separate access-control surfaces (see ARCHITECTURE.md #11):
- user auth (JWT)      -> /api/*
- webhook signatures   -> /webhooks/*  (machines, never JWT)
"""

import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from jose import JWTError, jwt

from app.core.config import settings

_ph = PasswordHasher()


# --- passwords -----------------------------------------------------------
def hash_password(plain: str) -> str:
    return _ph.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, plain)
    except VerifyMismatchError:
        return False


# --- JWT -----------------------------------------------------------------
def _create_token(sub: str, expires: timedelta, token_type: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": sub, "type": token_type, "iat": now, "exp": now + expires}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(sub: str) -> str:
    return _create_token(sub, timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES), "access")


def create_refresh_token(sub: str) -> str:
    return _create_token(sub, timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS), "refresh")


def decode_token(token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None


# --- short-lived signed playback URLs (recordings are sensitive; never expose raw paths) ---
def create_playback_token(recording_id: str, minutes: int = 5) -> str:
    return _create_token(recording_id, timedelta(minutes=minutes), "playback")


def verify_playback_token(token: str) -> str | None:
    payload = decode_token(token)
    if not payload or payload.get("type") != "playback":
        return None
    return payload.get("sub")


# --- webhook signatures --------------------------------------------------
def _hmac_sha1_b64(secret: str, url: str, params: dict[str, str]) -> str:
    """The Twilio-style scheme: base64(HMAC-SHA1(full_url + sorted concatenated params))."""
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    mac = hmac.new(secret.encode(), data.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(mac).decode()


def verify_twilio_signature(url: str, params: dict[str, str], signature: str) -> bool:
    if not settings.TWILIO_AUTH_TOKEN or not signature:
        return False
    return hmac.compare_digest(_hmac_sha1_b64(settings.TWILIO_AUTH_TOKEN, url, params), signature)


def verify_signalwire_signature(url: str, params: dict[str, str], signature: str) -> bool:
    """SignalWire's Compatibility (cXML) API uses the same HMAC-SHA1 scheme as Twilio
    but keyed by the SignalWire API token, and (depending on space/version) delivered
    under a different header. Kept as a SEPARATE verifier per ARCHITECTURE.md #12.

    TODO: confirm the exact header for your SignalWire space during integration
    (X-SignalWire-Signature vs the compatibility X-Twilio-Signature).
    """
    if not settings.SIGNALWIRE_AUTH_TOKEN or not signature:
        return False
    return hmac.compare_digest(
        _hmac_sha1_b64(settings.SIGNALWIRE_AUTH_TOKEN, url, params), signature
    )
