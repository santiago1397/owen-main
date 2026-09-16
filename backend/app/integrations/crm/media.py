"""Pictures on a CRM text: the outbound media store, and the inbound relay (2026-09-16).

Additive. Nothing here touches call handling, a flow, the Asterisk path or an existing
route's behaviour, and with `CRM_LINK_ENABLED` false none of it runs at all.

## Why OWEN has to publish an outbound picture at all

BulkVS sends an MMS by FETCHING the media itself, over the public internet, from a URL you
hand it in `/messageSend`. So somebody has to serve the customer's photograph to a carrier
that cannot authenticate. The CRM cannot: it has no public hostname of its own that the
carrier is expected to reach, and putting one there would mean a second public surface
serving customer content. OWEN already has one — `api.<APP_DOMAIN>`, which is where the
BulkVS webhooks land — and already holds the BulkVS credential. So the CRM hands OWEN the
bytes and gets back an opaque id; OWEN mints the URL and gives it to the carrier.

## The exposure this creates, stated plainly

For as long as the URL lives, ANYONE who holds it can fetch that one picture with no
credential. That is not a design flaw to be engineered away — it is what "MMS" means, and
the same is true of every Twilio, SignalWire or BulkVS media URL in existence. What can be
controlled is how wide and how long, so:

  * **unguessable** — a 192-bit `secrets.token_urlsafe(24)` id, plus an HMAC over
    `id|expiry` keyed on a secret the CRM never sees. A tampered expiry is refused;
    guessing is not on the table;
  * **short-lived** — `CRM_LINK_MEDIA_TTL_SECONDS`, 30 minutes by default. The carrier
    fetches within seconds of the send; the rest of the window is slack for a retry;
  * **one object** — the route serves exactly the one file that id names. There is no
    listing, no directory, no range of ids and no way to walk from one picture to another;
  * **GET only, no cookies, no CORS**, and `Cache-Control: private, no-store` so no shared
    cache keeps a copy after expiry;
  * **swept** — the worker deletes expired files, so an expired URL has nothing behind it
    even if the signature check were somehow wrong.

The alternative designs were considered and are worse: a permanent public URL (the exposure
never ends), a URL on the CRM (a second public surface, and the CRM has no route Traefik
sends the carrier to), or embedding the picture in the API call to BulkVS (their API does
not take one). This is the minimum that sends a picture at all.

## Inbound is not symmetric, and deliberately so

An inbound picture is never published. The CRM asks for it through the API-key-gated
`/api/crm-link/messages/{id}/media/{i}`, OWEN fetches the carrier's URL with the carrier's
own credential, and the bytes go back over the internal network. Nobody outside the two
systems can reach an inbound picture at any point. See `app/integrations/crm/api.py`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("integrations.crm.media")

# A media id is exactly what `secrets.token_urlsafe(24)` produces. Matched rather than
# trusted: the id becomes a path segment, and a path segment that came off the internet is
# never joined to a directory without being checked first.
_MEDIA_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

# The same six the CRM allows, and for the same reason: nothing executable is ever stored or
# served. The CRM has already sniffed the bytes; this is the second of the two independent
# checks, because "the other side checked" is not a check.
ALLOWED_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}

REFUSE_NOT_CONFIGURED = (
    "sending pictures is not set up in the phone system "
    "(CRM_LINK_MEDIA_PUBLIC_BASE_URL is unset)"
)
REFUSE_TYPE = "only pictures can be sent — jpeg, png, gif, webp or heic"
REFUSE_TOO_BIG = "that picture is larger than the phone system will send"


def sniff(data: bytes) -> str | None:
    """The image type of `data` from its magic number, or None.

    A copy of the CRM's `attachments.sniff`, on purpose: the two systems must agree about
    what an image is, and agreeing by both reading the bytes is stronger than agreeing
    because one of them believed the other's header.
    """
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs"):
            return "image/heic"
        if brand in (b"mif1", b"msf1"):
            return "image/heif"
    return None


@dataclass(frozen=True)
class MediaSettings:
    """The env half, projected the way `config.settings_view` projects the rest."""

    public_base_url: str = ""
    directory: str = "/data/recordings/crm-media"
    ttl_seconds: int = 1800
    max_bytes: int = 5 * 1024 * 1024
    max_per_message: int = 5
    secret: str = ""

    @property
    def configured(self) -> bool:
        """Both halves, or outbound pictures are off.

        A base URL with no secret would mint URLs nothing could verify, and a secret with
        no base URL has nothing to sign. Either missing means the feature answers a refusal
        with a sentence rather than sending something half-built at a real customer.
        """
        return bool(self.public_base_url and self.secret)


def settings_view(settings) -> MediaSettings:
    """Duck-typed with `getattr` defaults, like `config.settings_view`, so this module
    stays importable against a settings object that predates these fields."""
    secret = str(getattr(settings, "CRM_LINK_MEDIA_SECRET", "") or "")
    if not secret:
        # Falls back to the CRM-link API token. It is already a high-entropy shared secret
        # scoped to exactly this integration, the CRM never receives it (the CRM sends its
        # OWN key and gets an opaque id back), and rotating it rotates the signing key —
        # which is the correct behaviour, not an accident.
        secret = str(getattr(settings, "CRM_LINK_TOKEN", "") or "")
    return MediaSettings(
        public_base_url=str(
            getattr(settings, "CRM_LINK_MEDIA_PUBLIC_BASE_URL", "") or ""
        ).rstrip("/"),
        directory=str(getattr(settings, "CRM_LINK_MEDIA_DIR", "")
                      or "/data/recordings/crm-media"),
        ttl_seconds=int(getattr(settings, "CRM_LINK_MEDIA_TTL_SECONDS", 1800) or 1800),
        max_bytes=int(getattr(settings, "CRM_LINK_MEDIA_MAX_BYTES", 5 * 1024 * 1024)
                      or 5 * 1024 * 1024),
        max_per_message=int(getattr(settings, "CRM_LINK_MEDIA_MAX_PER_MESSAGE", 5) or 5),
        secret=secret,
    )


def current() -> MediaSettings:
    from app.core.config import settings

    return settings_view(settings)


# --- the store ----------------------------------------------------------------------------
#
# A directory of files, not a table. The bytes live for half an hour, they are written once
# and read once, and a `media` table would mean a migration against a live telephony
# database for data with a 30-minute lifetime.
#
# The DEFAULT directory is inside the `recordings` volume, which docker-compose.prod.yml
# already mounts on both the app and the worker. That is deliberate: a fresh deploy that
# changes no compose file still has a persistent place to put these, and a picture the
# carrier has not fetched yet does not vanish on the next `docker compose up`.


def _root(cfg: MediaSettings) -> Path:
    return Path(cfg.directory)


def _paths(cfg: MediaSettings, media_id: str) -> tuple[Path, Path]:
    root = _root(cfg)
    return root / media_id, root / (media_id + ".json")


def store(data: bytes, cfg: MediaSettings | None = None) -> tuple[str, str]:
    """Write one picture and return `(media_id, content_type)`.

    Raises ValueError with a sentence for anything refused — the route turns it into a 4xx.
    The type comes from SNIFFING, never from the uploader's header: this is the second of
    the two independent checks, and the one closest to the carrier.
    """
    cfg = cfg or current()
    if len(data) > cfg.max_bytes:
        raise ValueError(REFUSE_TOO_BIG)
    content_type = sniff(data)
    if content_type is None:
        raise ValueError(REFUSE_TYPE)

    media_id = secrets.token_urlsafe(24)
    blob, meta = _paths(cfg, media_id)
    blob.parent.mkdir(parents=True, exist_ok=True)
    tmp = blob.with_suffix(".part")
    tmp.write_bytes(data)
    os.replace(tmp, blob)
    meta.write_text(json.dumps({"content_type": content_type,
                                "byte_size": len(data),
                                "created_at": int(time.time())}))
    logger.info("crm-media: stored %s (%s, %d bytes)", media_id, content_type, len(data))
    return media_id, content_type


def read(media_id: str, cfg: MediaSettings | None = None) -> tuple[bytes, str] | None:
    """The bytes and content type, or None for anything at all that is not right.

    ONE answer for "no such id", "a malformed id", "swept" and "the volume is not
    mounted". The caller turns it into a single 404, so the public route cannot be used to
    tell which of those is true.
    """
    cfg = cfg or current()
    if not _MEDIA_ID.match(str(media_id or "")):
        return None
    blob, meta = _paths(cfg, media_id)
    try:
        data = blob.read_bytes()
        info = json.loads(meta.read_text())
    except (OSError, ValueError):
        return None
    content_type = str(info.get("content_type") or "")
    if content_type not in ALLOWED_TYPES:
        # A sidecar that does not name an allowed image type is not served. Serving it
        # would mean this route's Content-Type came from a file rather than from a check.
        return None
    return data, content_type


def forget(media_id: str, cfg: MediaSettings | None = None) -> bool:
    cfg = cfg or current()
    if not _MEDIA_ID.match(str(media_id or "")):
        return False
    gone = False
    for path in _paths(cfg, media_id):
        try:
            path.unlink()
            gone = True
        except OSError:
            pass
    return gone


def sweep(cfg: MediaSettings | None = None, now: float | None = None) -> int:
    """Delete everything past its expiry. Returns how many files went.

    Run by the worker. Belt and braces: an expired URL is already refused by its signature,
    and this makes sure there is nothing behind it either. A grace period of one TTL is
    allowed so a carrier retrying a late fetch is not defeated by a sweep that ran a second
    after the signature was still good.
    """
    cfg = cfg or current()
    cutoff = (now or time.time()) - (cfg.ttl_seconds * 2)
    root = _root(cfg)
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for path in entries:
        if path.suffix == ".json":
            continue
        try:
            if path.stat().st_mtime < cutoff:
                forget(path.name, cfg)
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("crm-media: swept %d expired picture(s)", removed)
    return removed


# --- the signed URL -----------------------------------------------------------------------


def _signature(secret: str, media_id: str, expires_at: int) -> str:
    mac = hmac.new(secret.encode(), f"{media_id}.{expires_at}".encode(), hashlib.sha256)
    # URL-safe base64 of the full digest, unpadded: 43 characters, 256 bits. Not truncated —
    # there is no length pressure on a query parameter, and a full digest is one less thing
    # to have to argue about.
    return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")


def signed_url(media_id: str, cfg: MediaSettings | None = None,
               now: float | None = None) -> tuple[str, int]:
    """`(url, expires_at)` for the carrier. Raises ValueError when the feature is not set up.

    The expiry is IN the URL and covered by the signature, so it cannot be extended by
    editing it; `verify` re-derives the signature over whatever expiry was presented.
    """
    cfg = cfg or current()
    if not cfg.configured:
        raise ValueError(REFUSE_NOT_CONFIGURED)
    expires_at = int((now or time.time()) + cfg.ttl_seconds)
    sig = _signature(cfg.secret, media_id, expires_at)
    return ("%s/crm-media/%s?e=%d&t=%s" % (cfg.public_base_url, media_id, expires_at, sig),
            expires_at)


def verify(media_id: str, expires: str | int | None, token: str | None,
           cfg: MediaSettings | None = None, now: float | None = None) -> bool:
    """Is this exactly a URL we minted, and is it still alive?

    `hmac.compare_digest` rather than `==`: the comparison runs on a public route, and a
    byte-by-byte one leaks the prefix of a valid signature through its timing.
    """
    cfg = cfg or current()
    if not cfg.configured or not _MEDIA_ID.match(str(media_id or "")):
        return False
    try:
        expires_at = int(expires)
    except (TypeError, ValueError):
        return False
    if expires_at < int(now or time.time()):
        return False
    return hmac.compare_digest(_signature(cfg.secret, media_id, expires_at),
                               str(token or ""))


# --- fetching an INBOUND picture from the carrier -----------------------------------------
#
# BulkVS delivers an MMS by putting media URLs in its MO webhook; `providers/bulkvs.py`
# `_media_list` has stored them on `messages.media_urls` since Ticket 09. They EXPIRE, which
# is why the CRM copies the bytes rather than linking to them, and this is the only place
# that ever dereferences one.
#
# ASSUMPTION, flagged rather than hidden: whether a BulkVS media URL needs the REST
# credential is NOT documented and has not been observed on this account — no inbound MMS
# has reached this code path since it was written. So the fetch is tried WITHOUT a
# credential first, which is correct for a pre-signed link, and retried WITH the existing
# `/tnRecord` Basic auth only on a 401/403 and only for a bulkvs.com host. Whichever it
# turns out to be, one of the two attempts is right, and the credential is never sent
# anywhere but BulkVS.

_MEDIA_FETCH_TIMEOUT = 20.0
_MEDIA_FETCH_MAX_BYTES = 12 * 1024 * 1024


async def fetch_carrier_media(url: str) -> tuple[bytes, str] | None:
    """`(bytes, content_type)` for one carrier media URL, or None.

    None for every failure — expired, refused, unreachable, not an image, too large. The
    caller answers 404 and the CRM shows "Picture unavailable" with a Retry beside a text
    that is otherwise perfectly intact, which is the behaviour the owner asked for: never
    lose the words over a picture.
    """
    import httpx

    from app.core.config import settings

    target = str(url or "").strip()
    if not target.lower().startswith(("http://", "https://")):
        logger.warning("crm-media: refusing to fetch a non-HTTP media URL")
        return None

    host = ""
    try:
        host = httpx.URL(target).host or ""
    except Exception:  # noqa: BLE001 - an unparseable URL is simply not fetched
        return None

    auth = None
    if host.lower().endswith("bulkvs.com"):
        auth = (settings.BULKVS_API_USERNAME, settings.BULKVS_API_PASSWORD)

    async with httpx.AsyncClient(timeout=_MEDIA_FETCH_TIMEOUT, follow_redirects=True) as c:
        try:
            resp = await c.get(target)
            if resp.status_code in (401, 403) and auth is not None:
                resp = await c.get(target, auth=auth)
        except Exception:  # noqa: BLE001 - one unreachable picture, never an exception here
            logger.warning("crm-media: could not fetch an inbound picture", exc_info=True)
            return None

    if resp.status_code >= 400:
        logger.info("crm-media: the carrier answered %d for an inbound picture",
                    resp.status_code)
        return None
    data = resp.content
    if len(data) > _MEDIA_FETCH_MAX_BYTES:
        logger.warning("crm-media: an inbound picture was %d bytes — not relayed", len(data))
        return None
    # Sniffed, not believed: the CRM will sniff it again, and a carrier's Content-Type is
    # the one thing on this path nobody here controls.
    content_type = sniff(data)
    if content_type is None:
        logger.info("crm-media: an inbound attachment is not an image — not relayed")
        return None
    return data, content_type
