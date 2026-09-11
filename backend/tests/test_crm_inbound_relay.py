"""Gap 2: an inbound text on a BOUND DID must reach the CRM — and nothing else may change.

`/webhooks/bulkvs/message` is a live surface: real customers text these numbers today, and
what it does with a text is ingest it, maintain the STOP/START opt-out state, and enqueue
`message_relay_ghl` — the REAL GoHighLevel relay. The CRM link is an ADDITION to that, never
a replacement, and this file's most important assertion is the boring one: the GoHighLevel
job is still enqueued, exactly once, with exactly the payload it has today, in every world.

Driven through the REAL route function `webhooks/bulkvs.py::message`. The database is faked
(there is none in this sandbox) but the ordering, the guards, the queue writes and the
webhook's 200 are all the real code. The CRM itself is mocked at the HTTP BOUNDARY in the
delivery-route tests at the bottom, exactly as `test_crm_unknown_caller.py` does — nothing
here ever contacts a live CRM.

Run: python -m tests.test_crm_inbound_relay
"""

import asyncio
import uuid

from tests.test_crm_unknown_caller import FakeCrmHttp, FakeResponse  # HTTP-boundary mock

BOUND_DID = "+15615550200"
UNBOUND_DID = "+15615550100"
CUSTOMER = "+15615559999"
SID = "bulkvs-abc123"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"crm_inbound_relay failed at: {name}")


# --- a database that answers the three queries this path makes ------------------------------

class FakeResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def first(self):
        return self._value


class FakeSession:
    """`binding.resolve` looks up a (CrmLink, Number) join; `hook.is_new_inbound_message`
    looks up a messages row by SID. Everything else answers None."""

    def __init__(self, binding_row=None, *, already_ingested=False):
        self.binding_row = binding_row
        self.already_ingested = already_ingested
        self.queries = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, stmt):
        text = str(stmt)
        self.queries.append(text)
        if "crm_links" in text:
            return FakeResult(self.binding_row)
        if "messages" in text:
            return FakeResult((uuid.uuid4(),) if self.already_ingested else None)
        return FakeResult(None)

    async def commit(self):
        return None

    def add(self, _obj):
        return None


def _bound_row():
    from app.integrations.crm.models import CrmLink
    from app.models import Number

    number = Number(phone_number=BOUND_DID, media_provider="asterisk",
                    friendly_name="CRM line")
    link = CrmLink(enabled=True, ring_operators=True, operator_ids=[], pstn_numbers=[],
                   ring_timeout_seconds=25)
    return (link, number)


class FakeMessage:
    """What `ingest_message_event` hands back — the `messages` row it just wrote."""

    def __init__(self, *, to_number, body="roof is leaking", num_media=0):
        self.id = uuid.uuid4()
        self.number_id = uuid.uuid4()
        self.from_number = CUSTOMER
        self.to_number = to_number
        self.body = body
        self.num_media = num_media
        self.provider_message_sid = SID


class FakeRequest:
    def __init__(self, payload):
        self._payload = payload
        self.query_params = {}

    async def json(self):
        return self._payload


# --- the harness ----------------------------------------------------------------------------

def run_webhook(*, enabled, bound, to_number=None, already_ingested=False,
                push_raises=False, body="roof is leaking", num_media=0):
    """Drive the REAL `/webhooks/bulkvs/message` route once.

    Returns `(status_code, enqueued_jobs)` where each job is `(type, payload)` — so the
    GoHighLevel relay and the CRM report are compared as the queue would see them.
    """
    import app.db as db_mod
    import app.integrations.crm.push as crm_push
    import app.services.queue as queue_mod
    import app.webhooks.bulkvs as hookmod
    from app.core.config import settings

    dialed = to_number or (BOUND_DID if bound else UNBOUND_DID)
    row = _bound_row() if bound else None
    session = FakeSession(row, already_ingested=already_ingested)
    msg = FakeMessage(to_number=dialed, body=body, num_media=num_media)
    jobs = []

    async def fake_verify(*_a, **_kw):
        return {}

    async def fake_ingest(_db, _provider, _evt):
        return msg

    async def fake_keyword(*_a, **_kw):
        return None

    async def fake_enqueue(_db, job_type, payload, delay_seconds=0):
        if push_raises and job_type == "crm_report":
            raise RuntimeError("the queue is having a bad day")
        jobs.append((job_type, payload))

    saved = (settings.CRM_LINK_ENABLED, settings.CRM_LINK_TOKEN, settings.AGENT_RUNTIME_KEY,
             hookmod.verify_request, hookmod.ingest_message_event,
             hookmod.apply_inbound_keyword, queue_mod.enqueue, hookmod.SessionLocal,
             db_mod.SessionLocal, crm_push.SessionLocal)
    try:
        settings.CRM_LINK_ENABLED = enabled
        settings.CRM_LINK_TOKEN = "ghl_pat_test"
        settings.AGENT_RUNTIME_KEY = "owen_sk_test"
        hookmod.verify_request = fake_verify
        hookmod.ingest_message_event = fake_ingest
        hookmod.apply_inbound_keyword = fake_keyword
        queue_mod.enqueue = fake_enqueue
        hookmod.SessionLocal = lambda: session
        db_mod.SessionLocal = lambda: session
        crm_push.SessionLocal = lambda: session
        resp = asyncio.run(hookmod.message(FakeRequest(
            {"From": CUSTOMER, "To": dialed, "Message": body})))
    finally:
        (settings.CRM_LINK_ENABLED, settings.CRM_LINK_TOKEN, settings.AGENT_RUNTIME_KEY,
         hookmod.verify_request, hookmod.ingest_message_event,
         hookmod.apply_inbound_keyword, queue_mod.enqueue, hookmod.SessionLocal,
         db_mod.SessionLocal, crm_push.SessionLocal) = saved
    return resp.status_code, jobs, msg


def _crm_jobs(jobs):
    return [p for t, p in jobs if t == "crm_report"]


def _ghl_jobs(jobs):
    return [p for t, p in jobs if t == "message_relay_ghl"]


# --- 1. THE REGRESSION: the GoHighLevel relay is untouched ------------------------------------

def test_the_existing_gohighlevel_relay_still_enqueues_exactly_as_it_does_today():
    """The one regression that would actually hurt. GoHighLevel is the live integration the
    business runs on; the CRM link is an addition to it and must be invisible to it."""
    print("message_relay_ghl is enqueued identically in all three worlds:")
    worlds = {
        "link off": run_webhook(enabled=False, bound=False),
        "link on, DID unbound": run_webhook(enabled=True, bound=False),
        "link on, DID BOUND": run_webhook(enabled=True, bound=True),
    }
    for name, (status, jobs, msg) in worlds.items():
        ghl = _ghl_jobs(jobs)
        check(f"{name}: the webhook answered 200", status == 200)
        check(f"{name}: exactly ONE message_relay_ghl job", len(ghl) == 1)
        check(f"{name}: carrying exactly the message id, and nothing else",
              ghl[0] == {"message_id": str(msg.id)})
        check(f"{name}: and it is the FIRST job enqueued, before anything CRM",
              jobs[0][0] == "message_relay_ghl")


# --- 2. an inbound text on a bound DID reaches the CRM ---------------------------------------

def test_an_inbound_sms_on_a_bound_did_is_reported_to_the_crm():
    print("an inbound text on a BOUND DID is queued for the CRM:")
    status, jobs, msg = run_webhook(enabled=True, bound=True)
    crm = _crm_jobs(jobs)

    check("the webhook still answered 200", status == 200)
    check("one CRM report was queued", len(crm) == 1)
    payload = crm[0]
    check("posted back to OWEN's message-events adapter, not straight to the CRM",
          payload["url"].endswith("/api/crm-link/message-events"))
    check("authenticated with the internal key, never a CRM token",
          payload["headers"] == {"X-OWEN-Key": "owen_sk_test"}
          and "ghl_pat_test" not in str(payload))
    facts = payload["body"]
    check("it carries messages.id — the join key", facts["owen_message_id"] == str(msg.id))
    check("the CUSTOMER's number", facts["caller_number"] == CUSTOMER)
    check("the bound DID it arrived on", facts["dialed_number"] == BOUND_DID)
    check("the text itself", facts["body"] == "roof is leaking")
    check("and the direction", facts["direction"] == "inbound")


def test_the_crm_body_that_inbound_text_becomes():
    print("the message facts map onto the CRM's SMS event:")
    from app.integrations.crm.events import (MessageEventFacts, to_crm_message_event,
                                             validate_crm_event)

    _s, jobs, msg = run_webhook(enabled=True, bound=True)
    facts = MessageEventFacts.from_payload(_crm_jobs(jobs)[0]["body"])
    body = to_crm_message_event(facts, contact_id=None)

    check("the CRM would accept the shape", validate_crm_event(body) == [])
    check("type is SMS", body["type"] == "SMS")
    check("direction is INBOUND, so the thread's unread badge is bumped",
          body["direction"] == "INBOUND")
    check("the customer's words, verbatim", body["body"] == "roof is leaking")
    check("from_number carries the customer, so a stranger gets a contact",
          body["from_number"] == CUSTOMER)
    check("provider_ref is messages.id", body["provider_ref"] == str(msg.id))
    check("and NO call_status — the CRM 400s a status on a non-CALL",
          "call_status" not in body)
    # An MMS has no words at all half the time; the picture IS the message.
    mms = to_crm_message_event(MessageEventFacts(
        owen_message_id="m", caller_number=CUSTOMER, body="", num_media=2))
    check("an MMS with no text still says something", mms["body"] == "[2 attachments — view in OWEN]")


# --- 3. an UNBOUND DID is untouched ------------------------------------------------------------

def test_an_inbound_sms_on_an_unbound_did_produces_no_crm_event():
    print("an inbound text on an UNBOUND DID reaches the CRM not at all:")
    off_status, off_jobs, _m1 = run_webhook(enabled=False, bound=False)
    on_status, on_jobs, _m2 = run_webhook(enabled=True, bound=False)

    check("link off: no CRM job", _crm_jobs(off_jobs) == [])
    check("link ON but DID unbound: still no CRM job", _crm_jobs(on_jobs) == [])
    check("both answered 200", off_status == 200 and on_status == 200)
    check("and the job sequence is identical with the module off and on",
          [t for t, _p in off_jobs] == [t for t, _p in on_jobs] == ["message_relay_ghl"])


def test_the_kill_switch_stops_a_bound_did_too():
    """The per-number opt-in is not the only switch. With CRM_LINK_ENABLED false a BOUND
    DID behaves exactly like an unbound one — and does not even ask the database."""
    print("with CRM_LINK_ENABLED false, even a BOUND DID reports nothing:")
    status, jobs, _msg = run_webhook(enabled=False, bound=True)
    check("no CRM job", _crm_jobs(jobs) == [])
    check("the GoHighLevel relay is untouched", len(_ghl_jobs(jobs)) == 1)
    check("200 as always", status == 200)

    # The stronger claim: the duplicate guard does not even query while the switch is off.
    import app.integrations.crm.hook as crm_hook
    from app.core.config import settings

    session = FakeSession(None)
    saved = settings.CRM_LINK_ENABLED
    try:
        settings.CRM_LINK_ENABLED = False
        off = asyncio.run(crm_hook.is_new_inbound_message(session, SID))
        off_queries = len(session.queries)
        settings.CRM_LINK_ENABLED = True
        on = asyncio.run(crm_hook.is_new_inbound_message(session, SID))
    finally:
        settings.CRM_LINK_ENABLED = saved
    check("disabled: it declines", off is False)
    check("disabled: with ZERO queries", off_queries == 0)
    check("enabled: a first sighting is reported as new", on is True)


# --- 4. failure must never cost the webhook its 200 -------------------------------------------

def test_a_broken_crm_push_does_not_break_message_handling():
    """A CRM that is down, or a queue write that fails, must not cost us the 200 — BulkVS
    re-delivers a POST it did not get one for, and the customer's text is already safe."""
    print("a failing CRM push leaves inbound message handling exactly as it was:")
    status, jobs, _msg = run_webhook(enabled=True, bound=True, push_raises=True)
    check("the webhook still answered 200", status == 200)
    check("the message was still relayed to GoHighLevel", len(_ghl_jobs(jobs)) == 1)
    check("and no CRM job was written", _crm_jobs(jobs) == [])


def test_a_redelivered_webhook_does_not_duplicate_the_text_on_the_crm_thread():
    """BulkVS retries an unacknowledged POST, and `POST /api/events` on the CRM always
    INSERTS. Without the pre-ingest check the customer's text would appear twice."""
    print("a re-delivered BulkVS webhook does not push the text twice:")
    _s1, first, _m1 = run_webhook(enabled=True, bound=True)
    _s2, again, _m2 = run_webhook(enabled=True, bound=True, already_ingested=True)

    check("the first delivery reported it", len(_crm_jobs(first)) == 1)
    check("the retry did NOT", _crm_jobs(again) == [])
    check("but the retry still ran the existing GoHighLevel relay, as it does today",
          len(_ghl_jobs(again)) == 1)


# --- 5. the delivery route, with the CRM mocked at the HTTP boundary --------------------------

def deliver_message(crm, *, bound=True, **kw):
    import app.integrations.crm.client as crm_client
    from app.core.config import settings
    from app.integrations.crm import api as crm_api

    payload = dict(owen_message_id="m-1", caller_number=CUSTOMER,
                   dialed_number=BOUND_DID, body="roof is leaking", direction="inbound")
    payload.update(kw)
    session = FakeSession(_bound_row() if bound else None)
    saved = (settings.CRM_LINK_ENABLED, settings.CRM_LINK_BASE_URL,
             settings.CRM_LINK_TOKEN, crm_client.httpx.AsyncClient)
    try:
        settings.CRM_LINK_ENABLED = True
        settings.CRM_LINK_BASE_URL = "http://ghl_clone_api:8000"
        settings.CRM_LINK_TOKEN = "ghl_pat_test"
        crm_client.httpx.AsyncClient = crm
        return asyncio.run(crm_api.deliver_message_event(
            crm_api.MessageDeliveryIn(**payload), session, None))
    finally:
        (settings.CRM_LINK_ENABLED, settings.CRM_LINK_BASE_URL,
         settings.CRM_LINK_TOKEN, crm_client.httpx.AsyncClient) = saved


def test_the_delivery_route_posts_the_text_to_the_crm():
    print("the queued message is delivered to the CRM as an SMS event:")
    crm = FakeCrmHttp(contacts=[])
    result = deliver_message(crm)

    check("one event was posted", len(crm.posted) == 1)
    body = crm.posted[0]
    check("as an SMS", body["type"] == "SMS")
    check("INBOUND", body["direction"] == "INBOUND")
    check("carrying the text", body["body"] == "roof is leaking")
    check("and the customer's number for a CRM that has never seen them",
          body["from_number"] == CUSTOMER)
    check("provider_ref is messages.id", body["provider_ref"] == "m-1")
    check("the route reports success", result.get("ok") is True)


def test_the_delivery_route_refuses_a_did_that_is_no_longer_bound():
    """A binding can be disabled between enqueue and drain. That is the point of having a
    per-number switch, and a job already in the queue must not outlive it."""
    print("a job for a DID that is no longer bound is refused at drain time:")
    crm = FakeCrmHttp(contacts=[])
    result = deliver_message(crm, bound=False)
    check("nothing was posted to the CRM", crm.posted == [])
    check("the job completes rather than retrying forever", result.get("ok") is False)
    check("and says why", "not bound" in str(result.get("reason")))


def test_a_crm_that_is_down_makes_the_queue_retry_rather_than_losing_the_text():
    print("a CRM 500 or an unreachable CRM is retried, not dropped:")
    from fastapi import HTTPException

    raised = None
    try:
        deliver_message(FakeCrmHttp(events_status=500))
    except HTTPException as exc:
        raised = exc
    check("a CRM 5xx raises 502 so the queue retries with backoff",
          raised is not None and raised.status_code == 502)

    class Unreachable(FakeCrmHttp):
        async def request(self, *_a, **_kw):
            raise OSError("name or service not known")

    raised = None
    try:
        deliver_message(Unreachable())
    except HTTPException as exc:
        raised = exc
    check("an unreachable CRM does the same", raised is not None and raised.status_code == 502)

    result = deliver_message(FakeCrmHttp(events_status=400))
    check("but a 4xx completes the job — it will not become acceptable on attempt six",
          result.get("ok") is False)


if __name__ == "__main__":
    test_the_existing_gohighlevel_relay_still_enqueues_exactly_as_it_does_today()
    test_an_inbound_sms_on_a_bound_did_is_reported_to_the_crm()
    test_the_crm_body_that_inbound_text_becomes()
    test_an_inbound_sms_on_an_unbound_did_produces_no_crm_event()
    test_the_kill_switch_stops_a_bound_did_too()
    test_a_broken_crm_push_does_not_break_message_handling()
    test_a_redelivered_webhook_does_not_duplicate_the_text_on_the_crm_thread()
    test_the_delivery_route_posts_the_text_to_the_crm()
    test_the_delivery_route_refuses_a_did_that_is_no_longer_bound()
    test_a_crm_that_is_down_makes_the_queue_retry_rather_than_losing_the_text()
    print("\nALL CRM INBOUND-RELAY CHECKS PASSED")
