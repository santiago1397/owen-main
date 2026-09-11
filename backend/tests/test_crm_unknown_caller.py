"""Gap 1, end to end: a first-time caller must reach the CRM.

The most valuable event a roofing business gets is a stranger calling the phone number.
Until now it was the one event OWEN could not deliver: `client.resolve_contact_id` searches
the CRM's contacts, a first-time caller is in none of them, and `api.deliver_event` then
returned `{"ok": false}` and dropped the event on the floor. The call was answered, rung,
recorded and filed in OWEN — and the CRM never heard about it.

The CRM's amendment (2026-09-11) accepts `from_number` instead of `contact_id`, matches it
on the last ten digits, and CREATES a contact when nothing matches. This file asserts the
owen-main half of that: the body carries the number, and an unresolved contact is no longer
a reason to stop.

The CRM is mocked at the HTTP BOUNDARY — `httpx.AsyncClient` inside
`integrations/crm/client.py` — so everything above it is the real code: the real route, the
real client, the real budget, the real body builder. Nothing here ever touches a live CRM.

Run: python -m tests.test_crm_unknown_caller
"""

import asyncio

DIALED = "+15615550200"
STRANGER = "+15615559999"
KNOWN = "+19415551234"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"crm_unknown_caller failed at: {name}")


# --- the CRM, mocked at the HTTP boundary ---------------------------------------------------

class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeCrmHttp:
    """A stand-in for `httpx.AsyncClient` that behaves like the real CRM.

    `contacts` is the CRM's contact table; the search is a substring ILIKE over the DISPLAY
    phone column, exactly as `GET /api/contacts?q=` is. Every POST is recorded so the test
    can assert on the body that would really have gone over the wire.
    """

    def __init__(self, contacts=(), *, events_status=201):
        self.contacts = list(contacts)
        self.events_status = events_status
        self.posted = []
        self.searched = []

    # httpx.AsyncClient(timeout=...) -> an async context manager
    def __call__(self, *_a, **_kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def request(self, method, url, headers=None, **kwargs):
        if method == "GET" and url.endswith("/api/contacts"):
            q = str((kwargs.get("params") or {}).get("q", ""))
            self.searched.append(q)
            items = [c for c in self.contacts
                     if q.lower() in str(c.get("phone") or "").lower()]
            return FakeResponse(200, {"items": items})
        if method == "POST" and url.endswith("/api/events"):
            body = kwargs.get("json") or {}
            self.posted.append(body)
            if self.events_status >= 400:
                return FakeResponse(self.events_status, {"detail": "refused"})
            return FakeResponse(self.events_status, {"id": 1, "conversation_id": 2})
        return FakeResponse(404, {"detail": f"unexpected {method} {url}"})


class FakeResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def first(self):
        return self._value


class FakeSession:
    """The route's `db`. The only query it makes is `binding.resolve`, and answering None
    means "this DID has no crm_links row", so the globals supply the URL and token."""

    async def execute(self, _stmt):
        return FakeResult(None)

    async def commit(self):
        return None


def _payload(**kw):
    base = dict(phase="ended", owen_call_id="3f1e0c22-0000-4000-8000-000000000abc",
                linkedid="1799000333.9", caller_number=STRANGER, dialed_number=DIALED,
                direction="inbound", outcome="answered", duration_seconds=134)
    base.update(kw)
    return base


def deliver(crm, **kw):
    """Drive the REAL `POST /api/crm-link/events` route against the mocked CRM."""
    import app.integrations.crm.client as crm_client
    from app.core.config import settings
    from app.integrations.crm import api as crm_api

    saved = (settings.CRM_LINK_ENABLED, settings.CRM_LINK_BASE_URL,
             settings.CRM_LINK_TOKEN, crm_client.httpx.AsyncClient)
    try:
        settings.CRM_LINK_ENABLED = True
        settings.CRM_LINK_BASE_URL = "http://ghl_clone_api:8000"
        settings.CRM_LINK_TOKEN = "ghl_pat_test"
        crm_client.httpx.AsyncClient = crm
        return asyncio.run(crm_api.deliver_event(
            crm_api.EventDeliveryIn(**_payload(**kw)), FakeSession(), None))
    finally:
        (settings.CRM_LINK_ENABLED, settings.CRM_LINK_BASE_URL,
         settings.CRM_LINK_TOKEN, crm_client.httpx.AsyncClient) = saved


# --- the tests ------------------------------------------------------------------------------

def test_a_stranger_is_delivered_instead_of_dropped():
    print("a caller who is in no CRM contact is DELIVERED, carrying from_number:")
    crm = FakeCrmHttp(contacts=[])            # nobody matches — a first-time lead
    result = deliver(crm)

    check("the CRM was searched first", crm.searched != [])
    check("nothing matched, and the event was sent anyway", len(crm.posted) == 1)
    body = crm.posted[0]
    check("the body carries the caller's number", body.get("from_number") == STRANGER)
    check("and names no contact_id", "contact_id" not in body)
    check("the route reports success", result.get("ok") is True)
    check("and reports no contact_id, honestly", result.get("contact_id") is None)
    check("it is the terminal CALL row, with the outcome on it",
          body.get("type") == "CALL" and body.get("call_status") == "completed")


def test_a_known_caller_is_still_filed_on_the_exact_contact():
    """The regression that would matter most: the path that works today."""
    print("a caller who IS a CRM contact is still filed against that contact:")
    crm = FakeCrmHttp(contacts=[{"id": 17, "phone": "(941) 555-1234"}])
    result = deliver(crm, caller_number=KNOWN)

    check("the event was sent", len(crm.posted) == 1)
    body = crm.posted[0]
    check("against the resolved contact", body.get("contact_id") == 17)
    check("with from_number alongside it", body.get("from_number") == KNOWN)
    check("and the route reports that contact", result.get("contact_id") == 17)


def test_a_token_without_the_read_scope_no_longer_loses_the_event():
    """A token scoped `events:write` alone cannot search contacts — the CRM 403s the lookup.
    That used to drop EVERY event, on every call, silently. Now the number carries it."""
    print("a write-only token can no longer lose an event:")

    class Forbidden(FakeCrmHttp):
        async def request(self, method, url, headers=None, **kwargs):
            if method == "GET":
                self.searched.append("denied")
                return FakeResponse(403, {"detail": "insufficient scope"})
            return await FakeCrmHttp.request(self, method, url, headers, **kwargs)

    crm = Forbidden()
    result = deliver(crm)
    check("the lookup was refused", crm.searched == ["denied"])
    check("the event was still delivered", len(crm.posted) == 1)
    check("carrying the number for the CRM to match or create",
          crm.posted[0].get("from_number") == STRANGER)
    check("and the route reports success", result.get("ok") is True)


def test_a_crm_5xx_is_still_retried_and_a_4xx_still_is_not():
    """The retry contract the worker reads must be unchanged by any of the above."""
    print("the retry contract is unchanged:")
    from fastapi import HTTPException

    raised = None
    try:
        deliver(FakeCrmHttp(events_status=500))
    except HTTPException as exc:
        raised = exc
    check("a CRM 5xx raises 502 so the queue retries",
          raised is not None and raised.status_code == 502)

    result = deliver(FakeCrmHttp(events_status=400))
    check("a CRM 4xx completes the job with ok:false", result.get("ok") is False)
    check("and says what the CRM said", "400" in str(result.get("reason")))


def test_the_kill_switch_still_refuses_the_whole_route():
    print("with CRM_LINK_ENABLED false the delivery route does nothing at all:")
    from fastapi import HTTPException

    from app.core.config import settings
    from app.integrations.crm import api as crm_api

    crm = FakeCrmHttp()
    saved = settings.CRM_LINK_ENABLED
    raised = None
    try:
        settings.CRM_LINK_ENABLED = False
        asyncio.run(crm_api.deliver_event(
            crm_api.EventDeliveryIn(**_payload()), FakeSession(), None))
    except HTTPException as exc:
        raised = exc
    finally:
        settings.CRM_LINK_ENABLED = saved

    check("it is a 503", raised is not None and raised.status_code == 503)
    check("and the CRM was never contacted", crm.posted == [] and crm.searched == [])


if __name__ == "__main__":
    test_a_stranger_is_delivered_instead_of_dropped()
    test_a_known_caller_is_still_filed_on_the_exact_contact()
    test_a_token_without_the_read_scope_no_longer_loses_the_event()
    test_a_crm_5xx_is_still_retried_and_a_4xx_still_is_not()
    test_the_kill_switch_still_refuses_the_whole_route()
    print("\nALL CRM UNKNOWN-CALLER CHECKS PASSED")
