"""The CRM browser softphone credential endpoint (`POST /api/crm-link/softphone/credentials`).

The owner's requirement: "i should be able to answer from the crm or the other 2 phones, the
one that picks up first, takes the call". The ring group already fans out to
`PJSIP/operator-<slug>` legs; the CRM was not one of them only because its users cannot reach
`POST /api/telephony/webrtc/credentials`, which is gated on OWEN's own app login.

What is asserted here, in the order it matters on a live phone system:

  1. **The kill switch refuses BEFORE anything happens.** Not "it returned 503" — that a
     disabled link never reaches the minting function at all, counted on a spy.
  2. **An unprovisioned email is refused**, by a distinct reason from "nobody is provisioned",
     and nothing is minted for it. An empty roster grants nothing.
  3. **A provisioned email gets credentials for the RIGHT operator** — the same slug
     `ring.py` dials and `pjsip.conf` declares, produced by the same `operator_slug`.
  4. **Nothing existing changes.** The login-time endpoint still requires `current_user`, the
     module's other routes still carry their own gates, and this route is the only addition.
  5. **No credential is ever logged.**

Run: python -m tests.test_crm_softphone_creds
"""

import asyncio
import logging

KNOWN = "owen@dreamteamroofingfl.com"
KNOWN_SLUG = "owen-dreamteamroofingfl.com"
UNKNOWN = "stranger@example.com"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"crm_softphone_creds failed at: {name}")


# --- 1. the pure roster kernel --------------------------------------------------------------

def test_roster_parsing():
    print("the operator roster parses like the destination allowlist:")
    from app.integrations.crm import softphone as sp

    check("empty roster is EMPTY (grants nothing)", sp.parse_roster("") == frozenset())
    check("None is empty too", sp.parse_roster(None) == frozenset())

    roster = sp.parse_roster(f" {KNOWN} , dispatch@x.com;\n tech@x.com ")
    check("comma, semicolon and newline all separate", len(roster) == 3)
    check("an email becomes its operator SLUG", KNOWN_SLUG in roster)
    check("spacing around an entry is ignored", "dispatch-x.com" in roster)

    check("case does not create a second operator",
          sp.parse_roster(f"{KNOWN},{KNOWN.upper()}") == frozenset({KNOWN_SLUG}))
    # operator_slug maps anything unusable to "unknown"; rostering that would make every junk
    # entry resolve to one shared operator.
    check("junk that slugs to 'unknown' is dropped, not rostered",
          sp.parse_roster("!!!, @@@") == frozenset())


def test_resolve_operator_distinguishes_its_refusals():
    print("resolving a CRM user to an operator, and the three ways it can refuse:")
    from app.integrations.crm import softphone as sp

    roster = sp.parse_roster(KNOWN)
    slug, refusal = sp.resolve_operator(KNOWN, roster)
    check("a rostered email resolves", refusal is None and slug == KNOWN_SLUG)

    slug, refusal = sp.resolve_operator("   ", roster)
    check("a blank email is refused as blank", refusal == sp.REFUSE_NO_EMAIL)

    slug, refusal = sp.resolve_operator(UNKNOWN, roster)
    check("an unrostered email is refused", refusal == sp.REFUSE_UNKNOWN_OPERATOR)
    check("...and the refusal names the provisioning it is missing",
          "pjsip.conf" in refusal and "CRM_LINK_SOFTPHONE_OPERATORS" in refusal)

    slug, refusal = sp.resolve_operator(KNOWN, frozenset())
    check("an EMPTY roster refuses even a plausible email",
          refusal == sp.REFUSE_NO_ROSTER)
    check("'nobody is provisioned' is a DIFFERENT reason from 'not this person'",
          sp.REFUSE_NO_ROSTER != sp.REFUSE_UNKNOWN_OPERATOR)


def test_the_slug_is_the_one_the_ring_group_dials():
    """The whole feature rests on this identity. If the CRM registers as one endpoint and
    `ring.py` originates to another, the browser is 'registered' and never rings."""
    print("the CRM registers as exactly the endpoint the ring group dials:")
    from app.integrations.crm import softphone as sp
    from app.telephony.credentials import operator_dial_endpoint

    slug, refusal = sp.resolve_operator(KNOWN, sp.parse_roster(KNOWN))
    check("no refusal", refusal is None)
    check("endpoint name matches ring.py's dial string",
          f"PJSIP/{sp.endpoint_for(slug)}" == operator_dial_endpoint(KNOWN))


def test_ttl_is_capped_and_floored():
    print("the CRM path is never the most generous door in the building:")
    from app.integrations.crm import softphone as sp

    check("the smaller of the two wins (cap lower)", sp.capped_ttl(900, 3600) == 900)
    check("the smaller of the two wins (platform lower)", sp.capped_ttl(900, 300) == 300)
    check("never below the floor", sp.capped_ttl(5, 3600) == sp.MIN_TTL_SECONDS)
    check("both unset degrades to the floor", sp.capped_ttl(0, 0) == sp.MIN_TTL_SECONDS)


# --- 2. the endpoint ------------------------------------------------------------------------

class MintSpy:
    """Wraps the REAL `build_webrtc_credentials` and records every call.

    A spy rather than a stub, because two different things are being asserted: that a refused
    request never reaches the minting function at ALL (count == 0), and that an allowed one
    gets the genuine article (so the TURN HMAC and endpoint naming are the real ones).
    """

    def __init__(self, real):
        self.real = real
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        return self.real(**kw)


class _World:
    """Settings the endpoint reads, restored on exit. Nothing here touches a database, a
    socket or a live Asterisk — the route has no `db` dependency by design."""

    FIELDS = ("CRM_LINK_ENABLED", "CRM_LINK_SOFTPHONE_OPERATORS",
              "CRM_LINK_SOFTPHONE_TTL_SECONDS", "ASTERISK_ENABLED", "OPERATOR_SIP_SECRET",
              "OPERATOR_SIP_DOMAIN", "OPERATOR_WSS_URL", "OPERATOR_SIP_TTL_SECONDS",
              "TURN_STATIC_SECRET", "TURN_URLS", "TURN_TTL_SECONDS")

    def __init__(self, **overrides):
        from app.core.config import settings

        self.settings = settings
        self.overrides = dict(
            CRM_LINK_ENABLED=True,
            CRM_LINK_SOFTPHONE_OPERATORS=KNOWN,
            CRM_LINK_SOFTPHONE_TTL_SECONDS=900,
            ASTERISK_ENABLED=True,
            OPERATOR_SIP_SECRET="sip-secret-never-logged",
            OPERATOR_SIP_DOMAIN="owen.example",
            OPERATOR_WSS_URL="wss://api.owen.example/ws",
            OPERATOR_SIP_TTL_SECONDS=3600,
            TURN_STATIC_SECRET="turn-secret-never-logged",
            TURN_URLS="turns:turn.owen.example:443?transport=tcp",
            TURN_TTL_SECONDS=3600,
        )
        self.overrides.update(overrides)
        self.saved = {}
        self.spy = None

    def __enter__(self):
        from app.integrations.crm import api as crm_api

        self.crm_api = crm_api
        for f in self.FIELDS:
            self.saved[f] = getattr(self.settings, f)
            setattr(self.settings, f, self.overrides[f])
        self.saved_mint = crm_api.build_webrtc_credentials
        self.spy = MintSpy(crm_api.build_webrtc_credentials)
        crm_api.build_webrtc_credentials = self.spy
        return self

    def __exit__(self, *exc):
        for f, v in self.saved.items():
            setattr(self.settings, f, v)
        self.crm_api.build_webrtc_credentials = self.saved_mint
        return False

    def call(self, email=KNOWN):
        """Invoke the endpoint function directly (FastAPI's Depends is bypassed; the auth
        gate itself is asserted separately in test_the_route_is_api_key_gated)."""
        body = self.crm_api.SoftphoneCredentialsIn(email=email)
        return asyncio.run(self.crm_api.softphone_credentials(body, _key=None))

    def refusal(self, email=KNOWN):
        from fastapi import HTTPException

        try:
            self.call(email)
        except HTTPException as exc:
            return exc
        return None


def test_kill_switch_refuses_before_minting_anything():
    print("the kill switch refuses BEFORE any credential is minted:")
    with _World(CRM_LINK_ENABLED=False) as w:
        exc = w.refusal()
        check("it refused", exc is not None)
        check("with 503", exc.status_code == 503)
        check("naming the switch", "CRM_LINK_ENABLED" in str(exc.detail))
        check("and NOTHING was minted", w.spy.calls == [])


def test_a_dark_telephony_platform_refuses_too():
    """Credentials for a platform that cannot place a call are a lie, not a courtesy — and
    the same 503 `POST /calls` already gives."""
    print("telephony disabled refuses, and mints nothing:")
    with _World(ASTERISK_ENABLED=False) as w:
        exc = w.refusal()
        check("503", exc is not None and exc.status_code == 503)
        check("nothing minted", w.spy.calls == [])


def test_an_unprovisioned_email_is_refused_and_no_operator_is_invented():
    print("an unknown CRM user is refused rather than given a fabricated operator:")
    with _World() as w:
        exc = w.refusal(UNKNOWN)
        check("403", exc is not None and exc.status_code == 403)
        check("the reason says what is missing",
              "CRM_LINK_SOFTPHONE_OPERATORS" in str(exc.detail))
        check("nothing minted for a stranger", w.spy.calls == [])

    with _World(CRM_LINK_SOFTPHONE_OPERATORS="") as w:
        exc = w.refusal(KNOWN)
        check("an EMPTY roster refuses the real operator too",
              exc is not None and exc.status_code == 403)
        check("still nothing minted", w.spy.calls == [])

    with _World() as w:
        exc = w.refusal("   ")
        check("a blank email is a 422, not a 403",
              exc is not None and exc.status_code == 422)
        check("nothing minted", w.spy.calls == [])


def test_a_provisioned_email_gets_credentials_for_the_right_operator():
    print("a provisioned CRM user gets credentials for their OWN operator endpoint:")
    from app.telephony.credentials import operator_dial_endpoint

    with _World() as w:
        out = w.call(KNOWN)
        check("it minted exactly once", len(w.spy.calls) == 1)
        check("the operator is the email's slug", out["operator"] == KNOWN_SLUG)
        check("the endpoint is operator-<slug>", out["endpoint"] == f"operator-{KNOWN_SLUG}")
        check("which is what ring.py dials",
              f"PJSIP/{out['endpoint']}" == operator_dial_endpoint(KNOWN))

        sip = out["sip"]
        check("SIP registers as that endpoint", sip["username"] == f"operator-{KNOWN_SLUG}")
        check("auth username matches the pjsip auth section",
              sip["authorization_username"] == f"operator-{KNOWN_SLUG}")
        check("the wss URL is handed over", sip["wss_url"] == "wss://api.owen.example/ws")
        check("TURN creds are present", len(out["ice_servers"]) == 1)
        check("the TURN username embeds this operator",
              out["ice_servers"][0]["username"].endswith(f":{KNOWN_SLUG}"))

        # Case must not mint a different operator — the CRM may send whatever the user typed.
        shouty = w.call(KNOWN.upper())
        check("a differently-cased email is the SAME operator",
              shouty["operator"] == KNOWN_SLUG)


def test_credentials_are_short_lived_and_never_longer_than_the_platforms():
    print("the credentials are short-lived, and shorter than the login-time path's:")
    import time

    with _World() as w:
        out = w.call()
        life = out["sip"]["expires_at"] - int(time.time())
        check("SIP expiry is the 900s cap, not the platform's 3600",
              890 <= life <= 900)
        turn_life = int(out["ice_servers"][0]["username"].split(":")[0]) - int(time.time())
        check("TURN expiry is capped the same way", 890 <= turn_life <= 900)

    # Lower the PLATFORM ttl below the cap: the platform still wins, so this path can never
    # outlive POST /api/telephony/webrtc/credentials for the same operator.
    with _World(OPERATOR_SIP_TTL_SECONDS=120, TURN_TTL_SECONDS=120) as w:
        out = w.call()
        life = out["sip"]["expires_at"] - int(time.time())
        check("a shorter platform TTL wins over the cap", 110 <= life <= 120)


def test_no_credential_is_ever_logged():
    """An `app_logs` reader (and `GET /api/ai/errors`) must never be able to read a SIP
    password or a TURN credential back out of a log line."""
    print("nothing secret reaches the log:")

    class Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    logger = logging.getLogger("integrations.crm.api")
    cap = Capture()
    logger.addHandler(cap)
    saved_level = logger.level
    # setLevel, not `logger.level = ...`: assigning the attribute leaves Logger._cache
    # holding the old answer, so an INFO call still short-circuits and the capture silently
    # sees only the WARNING.
    logger.setLevel(logging.DEBUG)
    try:
        with _World() as w:
            out = w.call()
            w.refusal(UNKNOWN)
    finally:
        logger.removeHandler(cap)
        logger.setLevel(saved_level)

    blob = "\n".join(cap.lines)
    check("something WAS logged (so this test has teeth)", len(cap.lines) >= 2)
    check("the SIP password is absent", out["sip"]["password"] not in blob)
    check("the TURN credential is absent", out["ice_servers"][0]["credential"] not in blob)
    check("the shared secrets are absent",
          "sip-secret-never-logged" not in blob and "turn-secret-never-logged" not in blob)
    check("the operator slug IS logged (this is what you debug with)", KNOWN_SLUG in blob)


# --- 3. isolation: nothing that already worked changed --------------------------------------

def test_the_route_is_api_key_gated_like_the_rest_of_the_module():
    print("the new route is gated by the CRM-link API key, not by an app login:")
    import inspect

    from app.api.ai.deps import require_scope
    from app.core.apikeys import SCOPE_CRM_LINK
    from app.integrations.crm import api as crm_api

    dep = inspect.signature(crm_api.softphone_credentials).parameters["_key"].default
    inner = dep.dependency
    closed_over = dict(zip(inner.__code__.co_freevars,
                           [c.cell_contents for c in (inner.__closure__ or ())]))
    check("the route declares a scope dependency", "scope" in closed_over)
    check("and the scope is crm_link", closed_over["scope"] == SCOPE_CRM_LINK)

    # Drive the real gate: a key without the scope is refused, one with it passes through.
    class FakeKey:
        def __init__(self, scopes):
            self.scopes = scopes

        def has(self, scope):
            return scope in self.scopes

    from fastapi import HTTPException

    gate = require_scope(SCOPE_CRM_LINK)
    denied = None
    try:
        asyncio.run(gate(key=FakeKey([])))
    except HTTPException as exc:
        denied = exc
    check("a key without crm_link is refused", denied is not None and denied.status_code == 403)
    check("a key WITH crm_link passes",
          asyncio.run(gate(key=FakeKey([SCOPE_CRM_LINK]))) is not None)

    # ...and it takes no database session, so a credential request cannot be affected by, or
    # affect, anything the call path is doing.
    check("the route has no db dependency",
          "db" not in inspect.signature(crm_api.softphone_credentials).parameters)


def test_the_login_time_endpoint_is_untouched():
    print("POST /api/telephony/webrtc/credentials still requires an OWEN app login:")
    import inspect

    from app.api import telephony as tele
    from app.api.deps import current_user

    dep = inspect.signature(tele.webrtc_credentials).parameters["user"].default
    check("it still depends on current_user", dep.dependency is current_user)
    src = inspect.getsource(tele.webrtc_credentials)
    check("it still gates on ASTERISK_ENABLED", "_require_enabled()" in src)
    check("it still mints for the LOGGED-IN user's email", "operator_id=user.email" in src)


def test_the_crm_link_router_gained_exactly_one_route():
    """A regression fence around 'additive'. If a future edit moves, renames or re-gates one
    of the existing routes, this fails rather than the deploy.

    Widened when feature/crm-link-relay merged: that branch added /message-events and
    /delivery-receipts, so the fence now names all seven. It CAUGHT the merge, which is
    exactly its job -- widen it deliberately, never delete it."""
    print("the crm-link router is exactly the routes we expect:")
    from app.integrations.crm import api as crm_api

    paths = sorted((r.path, tuple(sorted(r.methods))) for r in crm_api.router.routes)
    expected = sorted([
        ("/api/crm-link/events", ("POST",)),
        ("/api/crm-link/message-events", ("POST",)),
        ("/api/crm-link/delivery-receipts", ("POST",)),
        ("/api/crm-link/calls", ("POST",)),
        ("/api/crm-link/messages", ("POST",)),
        ("/api/crm-link/health", ("GET",)),
        ("/api/crm-link/softphone/credentials", ("POST",)),
    ])
    check("the route table is exactly the seven we expect, no more and no fewer",
          paths == expected)


def main():
    test_roster_parsing()
    test_resolve_operator_distinguishes_its_refusals()
    test_the_slug_is_the_one_the_ring_group_dials()
    test_ttl_is_capped_and_floored()
    test_kill_switch_refuses_before_minting_anything()
    test_a_dark_telephony_platform_refuses_too()
    test_an_unprovisioned_email_is_refused_and_no_operator_is_invented()
    test_a_provisioned_email_gets_credentials_for_the_right_operator()
    test_credentials_are_short_lived_and_never_longer_than_the_platforms()
    test_no_credential_is_ever_logged()
    test_the_route_is_api_key_gated_like_the_rest_of_the_module()
    test_the_login_time_endpoint_is_untouched()
    test_the_crm_link_router_gained_exactly_one_route()
    print("\nALL CRM SOFTPHONE CREDENTIAL CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        print(exc)
        raise
