"""Pictures on a CRM text (2026-09-16) — the media store, the signed URL, and the relay.

Behaviour, not status codes. Nothing here reaches BulkVS, the CRM or the network at all:
every HTTP call this module can make is replaced, and a test that tried would fail on an
unresolvable host rather than send anything.

What is proved, in the order it matters:

  1. **The signed URL is exactly what we minted, or it is refused.** A tampered expiry, a
     tampered signature, an expired one, a made-up id — all refused, and all refused the
     SAME way, so the public route cannot be used to learn which.
  2. **It expires**, and the sweep removes the bytes behind it too.
  3. **Only a picture is ever stored or served.** Decided by the magic number, not by the
     uploader's header.
  4. **An inbound picture is never published.** The relay route is API-key gated, refuses a
     DID that is not bound to the CRM, and the carrier's URL never leaves this process.
  5. **What BulkVS answers is kept.** `_extract_ref_id` now finds a nested id, and
     `send_result` returns the whole body so a real send writes the answer down.
  6. **Nothing existing moved**: the crm-link router has exactly its routes.

Run: python -m tests.test_crm_media
"""

import asyncio
import inspect
import sys
import time
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"crm_media failed at: {name}")


# --- real pictures, built rather than committed -------------------------------------------
# The code decides what a file is by SNIFFING it, so a test that fed it a text file named
# "roof.jpg" would prove nothing at all.

def png(tag: bytes = b"a") -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (len(payload).to_bytes(4, "big") + kind + payload
                + zlib.crc32(kind + payload).to_bytes(4, "big"))
    raw = b"".join(b"\x00" + (tag * 3) * 2 for _ in range(2))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", (2).to_bytes(4, "big") + (2).to_bytes(4, "big")
                    + bytes([8, 2, 0, 0, 0]))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF\x00\x01" + b"\x00" * 64 + b"\xff\xd9"
GIF = b"GIF89a" + b"\x01\x00\x01\x00" + b"\x00" * 20
WEBP = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"VP8 " + b"\x00" * 16
HEIC = b"\x00\x00\x00\x18" + b"ftyp" + b"heic" + b"\x00" * 16
NOT_AN_IMAGE = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\nstartxref\n"
SCRIPT = b"#!/bin/sh\nrm -rf /\n"


def settings_for(directory: str, **over):
    """A plain namespace standing in for the app's Settings — the shape
    `media.settings_view` is duck-typed against."""
    from types import SimpleNamespace

    values = {
        "CRM_LINK_MEDIA_PUBLIC_BASE_URL": "https://api.example.test",
        "CRM_LINK_MEDIA_DIR": directory,
        "CRM_LINK_MEDIA_TTL_SECONDS": 1800,
        "CRM_LINK_MEDIA_MAX_BYTES": 5 * 1024 * 1024,
        "CRM_LINK_MEDIA_MAX_PER_MESSAGE": 5,
        "CRM_LINK_MEDIA_SECRET": "a-signing-secret",
        "CRM_LINK_TOKEN": "the-crm-api-key",
    }
    values.update(over)
    return SimpleNamespace(**values)


# --- 1. what counts as a picture ----------------------------------------------------------


def test_only_pictures_and_only_by_their_bytes():
    from app.integrations.crm import media

    print("the type is decided by the magic number, never by a header:")
    check("jpeg", media.sniff(JPEG) == "image/jpeg")
    check("png", media.sniff(png()) == "image/png")
    check("gif", media.sniff(GIF) == "image/gif")
    check("webp", media.sniff(WEBP) == "image/webp")
    check("heic (an iPhone sends these)", media.sniff(HEIC) == "image/heic")
    check("a PDF is not a picture", media.sniff(NOT_AN_IMAGE) is None)
    check("a shell script is not a picture", media.sniff(SCRIPT) is None)
    check("empty is not a picture", media.sniff(b"") is None)
    check("a truncated header is not a picture", media.sniff(b"\x89PNG") is None)


def test_the_store_refuses_anything_that_is_not_a_picture_and_writes_nothing():
    from app.integrations.crm import media

    print("storing:")
    with TemporaryDirectory() as tmp:
        cfg = media.settings_view(settings_for(tmp))
        media_id, content_type = media.store(png(), cfg)
        check("a picture is stored and its type comes back", content_type == "image/png")
        check("the bytes come back byte for byte", media.read(media_id, cfg)[0] == png())

        before = sorted(Path(tmp).iterdir())
        for bad, why in ((NOT_AN_IMAGE, "a PDF"), (SCRIPT, "a shell script")):
            try:
                media.store(bad, cfg)
                check(f"{why} was refused", False)
            except ValueError as exc:
                check(f"{why} is refused with a sentence", "picture" in str(exc))
        check("and nothing was written for either", sorted(Path(tmp).iterdir()) == before)

        try:
            media.store(png() + b"\x00" * (cfg.max_bytes + 1), cfg)
            check("an oversize picture was refused", False)
        except ValueError as exc:
            check("an oversize picture is refused with a sentence", "larger" in str(exc))
        check("and nothing was written for that either",
              sorted(Path(tmp).iterdir()) == before)


# --- 2. the signed URL --------------------------------------------------------------------


def test_only_a_url_we_minted_is_accepted():
    from app.integrations.crm import media

    print("the signed URL:")
    with TemporaryDirectory() as tmp:
        cfg = media.settings_view(settings_for(tmp))
        media_id, _ = media.store(png(), cfg)
        url, expires_at = media.signed_url(media_id, cfg)

        check("the id is not guessable (192 bits, url-safe)", len(media_id) >= 30)
        check("the URL is on the configured public origin",
              url.startswith("https://api.example.test/crm-media/" + media_id))
        query = url.split("?", 1)[1]
        params = dict(p.split("=", 1) for p in query.split("&"))
        token = params["t"]

        check("the real thing verifies", media.verify(media_id, expires_at, token, cfg))
        check("a tampered SIGNATURE is refused",
              not media.verify(media_id, expires_at, token[:-2] + "aa", cfg))
        check("an extended EXPIRY is refused — the expiry is signed too",
              not media.verify(media_id, expires_at + 86400, token, cfg))
        check("no signature at all is refused",
              not media.verify(media_id, expires_at, "", cfg))
        check("another id with this signature is refused",
              not media.verify("Z" * 32, expires_at, token, cfg))
        check("an expired link is refused",
              not media.verify(media_id, expires_at, token, cfg,
                               now=expires_at + 1))
        check("a non-numeric expiry is refused",
              not media.verify(media_id, "soon", token, cfg))

        # A different deployment secret cannot verify our URLs, and ours cannot verify
        # theirs — which is what makes rotating the secret revoke every live link.
        other = media.settings_view(settings_for(tmp, CRM_LINK_MEDIA_SECRET="different"))
        check("a URL signed with another secret is refused",
              not media.verify(media_id, expires_at, token, other))


def test_with_nothing_configured_no_url_is_ever_minted():
    from app.integrations.crm import media

    print("off unless configured:")
    with TemporaryDirectory() as tmp:
        off = media.settings_view(settings_for(tmp, CRM_LINK_MEDIA_PUBLIC_BASE_URL="",
                                               CRM_LINK_MEDIA_SECRET="",
                                               CRM_LINK_TOKEN=""))
        check("not configured", not off.configured)
        try:
            media.signed_url("anything", off)
            check("minting was refused", False)
        except ValueError as exc:
            check("minting refuses with a sentence naming the setting",
                  "CRM_LINK_MEDIA_PUBLIC_BASE_URL" in str(exc))
        check("and nothing verifies either", not media.verify("x" * 32, 1, "t", off))


def test_the_secret_falls_back_to_the_crm_token_and_never_to_nothing():
    from app.integrations.crm import media

    print("the signing key:")
    with TemporaryDirectory() as tmp:
        cfg = media.settings_view(settings_for(tmp, CRM_LINK_MEDIA_SECRET=""))
        check("falls back to CRM_LINK_TOKEN", cfg.secret == "the-crm-api-key")
        blank = media.settings_view(settings_for(tmp, CRM_LINK_MEDIA_SECRET="",
                                                 CRM_LINK_TOKEN=""))
        check("with neither set the feature is OFF rather than unsigned",
              not blank.configured)


# --- 3. expiry is real, and the bytes go too ----------------------------------------------


def test_the_sweep_removes_the_bytes_behind_an_expired_link():
    from app.integrations.crm import media

    print("sweeping:")
    with TemporaryDirectory() as tmp:
        cfg = media.settings_view(settings_for(tmp))
        fresh, _ = media.store(png(b"a"), cfg)
        stale, _ = media.store(png(b"b"), cfg)
        # Age one of them past twice the TTL, which is the grace the sweep allows.
        old = time.time() - (cfg.ttl_seconds * 2) - 60
        import os
        for suffix in ("", ".json"):
            os.utime(Path(tmp) / (stale + suffix), (old, old))

        removed = media.sweep(cfg)
        check("the expired picture was swept", removed == 1)
        check("and its bytes are gone", media.read(stale, cfg) is None)
        check("the live one is untouched", media.read(fresh, cfg)[0] == png(b"a"))


def test_a_removed_picture_cannot_be_read_by_any_route():
    from app.integrations.crm import media

    print("forgetting:")
    with TemporaryDirectory() as tmp:
        cfg = media.settings_view(settings_for(tmp))
        media_id, _ = media.store(png(), cfg)
        check("it is there", media.read(media_id, cfg) is not None)
        check("forget removes it", media.forget(media_id, cfg))
        check("and it reads as gone", media.read(media_id, cfg) is None)


def test_a_media_id_is_never_joined_to_the_directory_unchecked():
    from app.integrations.crm import media

    print("path traversal:")
    with TemporaryDirectory() as tmp:
        cfg = media.settings_view(settings_for(tmp))
        for bad in ("../../etc/passwd", "/etc/passwd", "a/b", "..", "", "x" * 200):
            check(f"read({bad!r}) is refused", media.read(bad, cfg) is None)
            check(f"forget({bad!r}) is refused", media.forget(bad, cfg) is False)
            check(f"verify({bad!r}) is refused", not media.verify(bad, 9e9, "t", cfg))


# --- 4. the public route ------------------------------------------------------------------


def test_the_public_route_answers_one_picture_and_404s_everything_else():
    from app.integrations.crm import media, public_media

    print("GET /crm-media/{id}:")
    with TemporaryDirectory() as tmp:
        cfg = media.settings_view(settings_for(tmp))
        media_id, _ = media.store(png(), cfg)
        url, expires_at = media.signed_url(media_id, cfg)
        token = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))["t"]

        import app.integrations.crm.config as crm_config
        saved_enabled = crm_config.link_enabled
        saved_current = media.current
        crm_config.link_enabled = lambda: True
        public_media.crm_config.link_enabled = lambda: True
        public_media.crm_media.current = lambda: cfg
        try:
            ok = asyncio.run(public_media.fetch_media(media_id, str(expires_at), token))
            check("a valid link serves the picture", ok.status_code == 200)
            check("with the sniffed content type", ok.media_type == "image/png")
            check("the bytes are the picture", ok.body == png())
            check("no shared cache may keep it",
                  "no-store" in ok.headers.get("cache-control", ""))
            check("the browser is told not to sniff",
                  ok.headers.get("x-content-type-options") == "nosniff")
            check("and nothing indexes it", "noindex" in ok.headers.get("x-robots-tag", ""))

            same = []
            for label, args in (
                ("a tampered signature", (media_id, str(expires_at), token[:-2] + "aa")),
                ("an extended expiry", (media_id, str(expires_at + 9999), token)),
                ("an expired link", (media_id, "1", token)),
                ("an id that never existed", ("Z" * 32, str(expires_at), token)),
                ("no signature", (media_id, str(expires_at), "")),
                ("a traversal", ("../../etc/passwd", str(expires_at), token)),
            ):
                resp = asyncio.run(public_media.fetch_media(*args))
                check(f"{label} -> refused", resp.status_code == 404)
                same.append(resp.status_code)
            check("every refusal is the SAME answer, so it tells nothing apart",
                  len(set(same)) == 1)

            # A picture swept out from under a URL that is still signed correctly.
            media.forget(media_id, cfg)
            gone = asyncio.run(public_media.fetch_media(media_id, str(expires_at), token))
            check("a swept picture is 404 even with a good signature",
                  gone.status_code == 404)
        finally:
            crm_config.link_enabled = saved_enabled
            media.current = saved_current
            public_media.crm_config.link_enabled = saved_enabled
            public_media.crm_media.current = saved_current


def test_the_public_route_is_dark_while_the_kill_switch_is_off():
    from app.integrations.crm import config as crm_config
    from app.integrations.crm import public_media

    print("the kill switch:")
    saved = public_media.crm_config.link_enabled
    public_media.crm_config.link_enabled = lambda: False
    try:
        resp = asyncio.run(public_media.fetch_media("x" * 32, "9999999999", "t"))
        check("CRM_LINK_ENABLED=false -> 404 before anything is read",
              resp.status_code == 404)
    finally:
        public_media.crm_config.link_enabled = saved
    check("and the real switch still defaults off",
          crm_config.CrmLinkSettings().enabled is False)


def test_the_public_router_has_exactly_one_route_and_it_is_a_GET():
    from app.integrations.crm import public_media

    print("the public surface:")
    routes = [(r.path, tuple(sorted(r.methods))) for r in public_media.router.routes]
    check("exactly one route", len(routes) == 1)
    check("GET /crm-media/{media_id}, and nothing else",
          routes[0] == ("/crm-media/{media_id}", ("GET",)))


# --- 5. inbound is relayed, never published ------------------------------------------------


def test_an_inbound_picture_is_fetched_with_our_credential_and_never_handed_out():
    from app.integrations.crm import media

    print("relaying an inbound picture:")
    calls = []

    class FakeResponse:
        def __init__(self, status_code, content=b"", ):
            self.status_code = status_code
            self.content = content

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            calls.append({"url": url, "auth": kw.get("auth")})
            if "expired" in url:
                return FakeResponse(404)
            if "notimage" in url:
                return FakeResponse(200, NOT_AN_IMAGE)
            if "needsauth" in url and kw.get("auth") is None:
                return FakeResponse(401)
            return FakeResponse(200, png())

    import httpx
    saved_client, saved_url = httpx.AsyncClient, httpx.URL
    httpx.AsyncClient = FakeClient
    try:
        got = asyncio.run(media.fetch_carrier_media("https://mms.bulkvs.com/abc.jpg"))
        check("the bytes come back", got is not None and got[0] == png())
        check("with the type we sniffed, not the carrier's header", got[1] == "image/png")
        # A pre-signed carrier link needs no credential, so none is offered on the first
        # try. Sending one unasked would put the BulkVS password on the wire on every
        # fetch, for a URL that did not want it.
        check("no credential is sent unless the carrier asks for one",
              len(calls) == 1 and calls[-1]["auth"] is None)

        calls.clear()
        again = asyncio.run(media.fetch_carrier_media("https://mms.bulkvs.com/needsauth.jpg"))
        check("a 401 is retried WITH the BulkVS credential and succeeds",
              again is not None and len(calls) == 2 and calls[1]["auth"] is not None)

        calls.clear()
        asyncio.run(media.fetch_carrier_media("https://cdn.somewhere-else.test/needsauth.jpg"))
        check("a NON-BulkVS host is never offered the BulkVS credential, even on a 401",
              all(c["auth"] is None for c in calls))

        check("an expired carrier link is None, never an exception",
              media.fetch_carrier_media is not None
              and asyncio.run(media.fetch_carrier_media(
                  "https://mms.bulkvs.com/expired.jpg")) is None)
        check("a carrier attachment that is not an image is not relayed",
              asyncio.run(media.fetch_carrier_media(
                  "https://mms.bulkvs.com/notimage.bin")) is None)
        check("a non-HTTP URL is never fetched at all",
              asyncio.run(media.fetch_carrier_media("file:///etc/passwd")) is None)
    finally:
        httpx.AsyncClient, httpx.URL = saved_client, saved_url


def test_the_relay_route_is_key_gated_and_bound_did_only():
    """Read from the source, because the check is the route's first act and there is no
    database here to drive it against."""
    from app.core.apikeys import SCOPE_CRM_LINK
    from app.integrations.crm import api as crm_api

    print("GET /api/crm-link/messages/{id}/media/{i}:")
    src = inspect.getsource(crm_api.message_media)
    check("it is behind require_scope(crm_link)",
          "require_scope(SCOPE_CRM_LINK)" in inspect.getsource(crm_api)
          and "_key=Depends(require_scope(SCOPE_CRM_LINK))" in src)
    check("the kill switch is asked first", src.index("_require_enabled()") < src.index("db.get"))
    check("only an INBOUND message", '"inbound"' in src)
    check("only a DID bound to the CRM", "crm_binding.resolve" in src)
    check("an index off the end is 404", "no such picture on that message" in src)
    check("the carrier's URL is never returned, only the bytes",
          "Response(content=data" in src and "urls[index]" in src
          and "media_urls" not in src.split("Response(content=data")[1])
    check("the scope constant is the CRM's own", SCOPE_CRM_LINK == "crm_link")


def test_nothing_publishes_an_inbound_picture():
    """The asymmetry, asserted rather than assumed: the only caller of `store()` — which
    is what makes a picture publicly fetchable — is the CRM's own upload route."""
    from app.integrations.crm import api as crm_api

    print("inbound is never published:")
    relay = inspect.getsource(crm_api.message_media)
    check("the relay route never stores anything publicly",
          "crm_media.store" not in relay and "signed_url" not in relay)
    upload = inspect.getsource(crm_api.upload_media)
    check("only the upload route stores", "crm_media.store" in upload)


# --- 6. the outbound send -----------------------------------------------------------------


def test_media_ids_become_signed_urls_and_a_plain_text_is_unchanged():
    from app.integrations.crm import api as crm_api

    print("POST /api/crm-link/messages:")
    src = inspect.getsource(crm_api.send_message)
    check("an empty body with a picture is still a message",
          "if not text and not media_ids" in src)
    check("an unknown or swept picture stops the send before anything is queued",
          "no longer on the phone system" in src
          and src.index("no longer on the phone system") < src.index("enqueue_outbound_message"))
    check("the per-message cap is enforced here too", "max_per_message" in src)
    check("outbound pictures refuse with a sentence when not configured",
          "REFUSE_NOT_CONFIGURED" in src)
    check("the URLs handed to the carrier are the ones WE signed",
          "crm_media.signed_url" in src)

    body = crm_api.SendMessageIn(from_number="+19544829099", to_number="+19415550101",
                                 body="hi")
    check("a text with no pictures declares none", body.media_ids == [])


def test_the_upload_route_obeys_the_same_dark_switch_a_text_does():
    from app.integrations.crm import api as crm_api

    print("POST /api/crm-link/media:")
    src = inspect.getsource(crm_api.upload_media)
    check("the kill switch first", "_require_enabled()" in src)
    check("then the SMS dark switch — a picture nobody can text is not stored",
          "REFUSE_SMS_DARK" in src)
    check("then the media configuration", "REFUSE_NOT_CONFIGURED" in src)
    check("the read is capped before the bytes are held",
          "read(media_cfg.max_bytes + 1)" in src)
    check("it answers an id, never the URL",
          '"media_id": media_id' in src and "signed_url" not in src)


def test_an_outbound_row_records_the_pictures_it_carries():
    from app.services import messages as messages_svc

    print("the outbound messages row:")
    src = inspect.getsource(messages_svc.enqueue_outbound_message)
    check("num_media counts them", "num_media=len(media_urls or [])" in src)
    check("and the URLs are kept on the row", "media_urls=list(media_urls or [])" in src)
    check("a text with no pictures is still num_media=0",
          len([]) == 0)


# --- 7. what BulkVS actually answers -------------------------------------------------------


def test_the_ref_id_is_found_wherever_bulkvs_puts_it():
    from app.providers.bulkvs_client import _extract_ref_id

    print("RefId extraction (widened after the first live send returned none):")
    check("top level, as before", _extract_ref_id({"RefId": "abc123"}) == "abc123")
    check("the MessageRef alias", _extract_ref_id({"MessageRef": "z9"}) == "z9")
    check("missing -> None", _extract_ref_id({"nope": 1}) is None)
    check("a bare list of strings -> None", _extract_ref_id(["x"]) is None)
    # The shapes the OLD version could not see, and the reason the CRM's bubble stuck.
    check("nested in a per-recipient Results list",
          _extract_ref_id({"Results": [{"To": "1941", "Status": "SUCCESS",
                                        "RefId": "nested-1"}]}) == "nested-1")
    check("a bare list of result objects",
          _extract_ref_id([{"To": "1941", "RefId": "listed-1"}]) == "listed-1")
    check("lower-case refid", _extract_ref_id({"refid": "lower"}) == "lower")
    check("an object under a ref-shaped key is not an id",
          _extract_ref_id({"Id": {"deep": "x"}, "Results": [{"RefId": "r"}]}) == "r")
    check("nothing anywhere -> None",
          _extract_ref_id({"Status": "SUCCESS", "Results": [{"To": "1941"}]}) is None)
    check("a cycle-free depth limit, so a pathological body cannot hang",
          _extract_ref_id({"a": {"b": {"c": {"d": {"e": {"RefId": "deep"}}}}}}) is None)


def test_a_send_keeps_the_whole_response_so_the_question_gets_answered():
    from app.providers import bulkvs_client

    print("send_result:")
    src = inspect.getsource(bulkvs_client.send_result)
    check("the decoded body is returned, not discarded", "SendResult(_extract_ref_id" in src)
    check("an unparseable body is kept as text rather than thrown away", '"_text"' in src)
    check("MediaURLs is what makes it an MMS", 'payload["MediaURLs"]' in src)
    old = inspect.getsource(bulkvs_client.send_message)
    check("the old one-value shape still exists for callers that only want the id",
          "ref_id" in old)


def test_the_crm_is_told_the_text_went_without_needing_a_ref_id():
    """The bug this fixes: BulkVS returned no RefId on the first live send, the
    delivery-status webhook had nothing to match on, and the CRM's bubble said "queued"
    for ever. OWEN now reports what IT knows — the carrier took it."""
    from app.workers import handlers

    print("handle_message_send:")
    src = inspect.getsource(handlers.handle_message_send)
    check("the response is stored on the row", '"bulkvs_send_response"' in src)
    check("and stored BESIDE the CRM-link marker, never over it",
          "dict(msg.raw_payload or {})" in src)
    check("the CRM is told 'sent' from our own knowledge",
          'handle_delivery_receipt(message_id=str(msg.id), status="sent")' in src)
    check("after the commit, so a failed relay cannot lose the send",
          src.index("await db.commit()") < src.index("handle_delivery_receipt"))
    check("it reports 'sent' — the carrier has taken it, nothing says it arrived",
          'status="sent"' in src and 'status="delivered"' not in src)


def test_the_delivery_hook_still_only_relays_the_crms_own_messages():
    """A `sent` for every text on the DID would be a 404 per operator message, forever."""
    from app.integrations.crm import hook

    print("the receipt hook is unchanged where it matters:")
    src = inspect.getsource(hook.handle_delivery_receipt)
    check("the kill switch first", "link_enabled()" in src)
    check("the CRM-link marker is required", "marker_of(msg.raw_payload)" in src)
    check("outbound only", '"outbound"' in src)
    check("a DID that is no longer bound is not relayed", "crm_binding.resolve" in src)
    check("and it is total — it can never raise into the caller", "except Exception" in src)


# --- 8. nothing existing moved -------------------------------------------------------------


def test_the_crm_link_router_gained_exactly_two_routes():
    """The same fence `test_crm_softphone_creds` keeps, widened DELIBERATELY. Both new
    routes are `crm_link` scope and both are about pictures; everything else is where it
    was, with the same methods."""
    from app.integrations.crm import api as crm_api

    print("the crm-link route table:")
    paths = sorted((r.path, tuple(sorted(r.methods))) for r in crm_api.router.routes)
    expected = sorted([
        ("/api/crm-link/events", ("POST",)),
        ("/api/crm-link/message-events", ("POST",)),
        ("/api/crm-link/delivery-receipts", ("POST",)),
        ("/api/crm-link/calls", ("POST",)),
        ("/api/crm-link/messages", ("POST",)),
        ("/api/crm-link/health", ("GET",)),
        ("/api/crm-link/softphone/credentials", ("POST",)),
        ("/api/crm-link/email-jobs", ("POST",)),
        ("/api/crm-link/media", ("POST",)),
        ("/api/crm-link/messages/{message_id}/media/{index}", ("GET",)),
    ])
    check("exactly the ten we expect, no more and no fewer", paths == expected)


def test_the_crm_event_now_carries_how_many_pictures_and_still_no_urls():
    from app.integrations.crm.events import MessageEventFacts, to_crm_message_event

    print("the CRM message event:")
    facts = MessageEventFacts(owen_message_id="m-1", caller_number="+19415550101",
                              dialed_number="+19544829099", body="roof", num_media=3)
    body = to_crm_message_event(facts)
    check("num_media is relayed", body["num_media"] == 3)
    check("the join key is messages.id", body["provider_ref"] == "m-1")
    check("NO media URL is ever sent to the CRM",
          not any("url" in str(k).lower() for k in body))
    check("the MMS note is still appended, for a CRM that cannot show pictures",
          "[3 attachments — view in OWEN]" in body["body"])
    check("the customer's own words are still first",
          body["body"].startswith("roof "))
    plain = to_crm_message_event(MessageEventFacts(owen_message_id="m-2", body="hello"))
    check("an ordinary text says zero and reads exactly as it always did",
          plain["num_media"] == 0 and plain["body"] == "hello")


def test_the_inbound_webhook_path_is_untouched_apart_from_the_count():
    from app.webhooks import bulkvs as bulkvs_webhooks

    print("the MO webhook:")
    src = inspect.getsource(bulkvs_webhooks.message)
    check("the GHL relay is still enqueued first",
          src.index("message_relay_ghl") < src.index("crm_hook.handle_inbound_message"))
    check("the CRM hook is still last and still guarded by first-sight",
          "if crm_first_sight:" in src)
    check("it still answers 200 whatever the CRM is doing",
          src.rstrip().endswith("return Response(status_code=200)"))


def main():
    test_only_pictures_and_only_by_their_bytes()
    test_the_store_refuses_anything_that_is_not_a_picture_and_writes_nothing()
    test_only_a_url_we_minted_is_accepted()
    test_with_nothing_configured_no_url_is_ever_minted()
    test_the_secret_falls_back_to_the_crm_token_and_never_to_nothing()
    test_the_sweep_removes_the_bytes_behind_an_expired_link()
    test_a_removed_picture_cannot_be_read_by_any_route()
    test_a_media_id_is_never_joined_to_the_directory_unchecked()
    test_the_public_route_answers_one_picture_and_404s_everything_else()
    test_the_public_route_is_dark_while_the_kill_switch_is_off()
    test_the_public_router_has_exactly_one_route_and_it_is_a_GET()
    test_an_inbound_picture_is_fetched_with_our_credential_and_never_handed_out()
    test_the_relay_route_is_key_gated_and_bound_did_only()
    test_nothing_publishes_an_inbound_picture()
    test_media_ids_become_signed_urls_and_a_plain_text_is_unchanged()
    test_the_upload_route_obeys_the_same_dark_switch_a_text_does()
    test_an_outbound_row_records_the_pictures_it_carries()
    test_the_ref_id_is_found_wherever_bulkvs_puts_it()
    test_a_send_keeps_the_whole_response_so_the_question_gets_answered()
    test_the_crm_is_told_the_text_went_without_needing_a_ref_id()
    test_the_delivery_hook_still_only_relays_the_crms_own_messages()
    test_the_crm_link_router_gained_exactly_two_routes()
    test_the_crm_event_now_carries_how_many_pictures_and_still_no_urls()
    test_the_inbound_webhook_path_is_untouched_apart_from_the_count()
    print("\nALL CRM MEDIA CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        print(exc)
        sys.exit(1)
