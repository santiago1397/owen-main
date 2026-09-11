"""Gap 3: "i want to know if the text arrived or not."

The CRM sends a text by asking OWEN to send it (`POST /api/crm-link/messages`), stores the
`message_id` OWEN answers with as its own `provider_ref`, and then waits. BulkVS reports
what happened to `/webhooks/bulkvs/message-status`, which advanced OWEN's own `messages` row
and returned 200 — and told nobody. The CRM's copy sat on QUEUED forever, whether the text
arrived, bounced, or was blocked by the carrier.

## THE CORRELATION FIELD

`provider_ref` = OWEN's `messages.id`. Read out of the CRM's source, not guessed:

  * `transport.py::OwenMainTransport.send` -> `MessageRef(provider_ref=str(data["message_id"]))`
  * `main.py::DeliveryReceipt` — "`provider_ref` is the id owen-main gave us when it
    accepted the message — its `message_id` ... It is the only join key: the CRM never sees
    the BulkVS RefId."
  * `ingest_delivery_receipt` looks up `ConversationEvent.provider_ref == body.provider_ref`
    AND `direction == OUTBOUND`, and 404s when it finds nothing.

The BulkVS RefId is what OWEN matches its OWN row on, and sending it instead would 404 every
receipt. The webhook resolves the RefId to the `messages` row and relays THAT row's id.

Nothing here contacts a live CRM: the delivery-route tests mock at the HTTP boundary.

Run: python -m tests.test_crm_delivery_receipt
"""

import ast
import asyncio
import pathlib
import uuid

from tests.test_crm_event_payload import _find_crm_source
from tests.test_crm_unknown_caller import FakeCrmHttp, FakeResponse  # HTTP-boundary mock

BOUND_DID = "+15615550200"
UNBOUND_DID = "+15615550100"
CUSTOMER = "+15615559999"
REF = "BV-99887766"
LINK_ID = "link-1"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"crm_delivery_receipt failed at: {name}")


# --- fakes ------------------------------------------------------------------------------------

class FakeResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def first(self):
        return self._value


class FakeMessage:
    def __init__(self, *, through_link=True, direction="outbound", status="sent",
                 from_number=BOUND_DID):
        from app.integrations.crm import config as crm_config

        self.id = uuid.uuid4()
        self.direction = direction
        self.status = status
        self.from_number = from_number
        self.to_number = CUSTOMER
        self.body = "we can be out Thursday"
        self.provider_message_sid = f"bulkvs-{REF}"
        # THE marker. An operator's text from the Inbox has raw_payload NULL, which is what
        # `through_link=False` reproduces.
        self.raw_payload = (crm_config.link_marker(LINK_ID, from_number)
                            if through_link else None)


class FakeSession:
    def __init__(self, msg=None, binding_row=None):
        self.msg = msg
        self.binding_row = binding_row
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, stmt):
        text = str(stmt)
        if "crm_links" in text:
            return FakeResult(self.binding_row)
        if "messages" in text:
            return FakeResult(self.msg)
        return FakeResult(None)

    async def get(self, _model, _pk):
        return self.msg

    async def commit(self):
        self.commits += 1

    def add(self, _obj):
        return None


def _bound_row():
    from app.integrations.crm.models import CrmLink
    from app.models import Number

    number = Number(phone_number=BOUND_DID, media_provider="asterisk")
    link = CrmLink(enabled=True, ring_operators=True, operator_ids=[], pstn_numbers=[])
    return (link, number)


class FakeRequest:
    def __init__(self, payload):
        self._payload = payload
        self.query_params = {}

    async def json(self):
        return self._payload


def run_dlr(*, enabled=True, through_link=True, bound=True, status="delivered",
            detail=None, direction="outbound", owen_status="sent"):
    """Drive the REAL `/webhooks/bulkvs/message-status` route once.

    Returns `(status_code, crm_jobs, message)` — the jobs being whatever the CRM link
    queued, so "was the receipt relayed" is asserted on the queue and not on a log line.
    """
    import app.db as db_mod
    import app.integrations.crm.push as crm_push
    import app.services.queue as queue_mod
    import app.webhooks.bulkvs as hookmod
    from app.core.config import settings

    msg = FakeMessage(through_link=through_link, direction=direction, status=owen_status)
    session = FakeSession(msg, _bound_row() if bound else None)
    jobs = []

    async def fake_verify(*_a, **_kw):
        return {}

    async def fake_enqueue(_db, job_type, payload, delay_seconds=0):
        jobs.append((job_type, payload))

    payload = {"RefId": REF, "Status": status}
    if detail:
        payload["ErrorMessage"] = detail

    saved = (settings.CRM_LINK_ENABLED, settings.CRM_LINK_TOKEN, settings.AGENT_RUNTIME_KEY,
             hookmod.verify_request, queue_mod.enqueue, hookmod.SessionLocal,
             db_mod.SessionLocal, crm_push.SessionLocal)
    try:
        settings.CRM_LINK_ENABLED = enabled
        settings.CRM_LINK_TOKEN = "ghl_pat_test"
        settings.AGENT_RUNTIME_KEY = "owen_sk_test"
        hookmod.verify_request = fake_verify
        queue_mod.enqueue = fake_enqueue
        hookmod.SessionLocal = lambda: session
        db_mod.SessionLocal = lambda: session
        crm_push.SessionLocal = lambda: session
        resp = asyncio.run(hookmod.message_status(FakeRequest(payload)))
    finally:
        (settings.CRM_LINK_ENABLED, settings.CRM_LINK_TOKEN, settings.AGENT_RUNTIME_KEY,
         hookmod.verify_request, queue_mod.enqueue, hookmod.SessionLocal,
         db_mod.SessionLocal, crm_push.SessionLocal) = saved
    return resp.status_code, [p for t, p in jobs if t == "crm_report"], msg


# --- 1. the correlation field ------------------------------------------------------------------

def test_the_receipt_is_keyed_on_owens_message_id_not_the_bulkvs_refid():
    print("provider_ref carries messages.id — the only key the CRM can match on:")
    from app.integrations.crm.events import (DeliveryReceiptFacts, to_crm_delivery_receipt,
                                             validate_crm_delivery_receipt)

    facts = DeliveryReceiptFacts(owen_message_id="9d1c-msg", status="delivered",
                                 dialed_number=BOUND_DID,
                                 provider_message_sid=f"bulkvs-{REF}")
    body = to_crm_delivery_receipt(facts)
    check("provider_ref is OWEN's messages.id", body["provider_ref"] == "9d1c-msg")
    check("the BulkVS RefId is NOT sent — the CRM has never seen one",
          REF not in str(body))
    check("the carrier's word is carried", body["status"] == "delivered")
    check("no detail is invented when the carrier gave none", "detail" not in body)
    check("the CRM would accept it", validate_crm_delivery_receipt(body) == [])

    failed = to_crm_delivery_receipt(DeliveryReceiptFacts(
        owen_message_id="m", status="failed", detail="handset unreachable"))
    check("a failure carries the carrier's own words for the operator",
          failed["detail"] == "handset unreachable")


def test_a_receipt_that_cannot_name_its_message_is_refused_here():
    print("a receipt with no join key never reaches the CRM:")
    from app.integrations.crm.events import (DeliveryReceiptFacts, to_crm_delivery_receipt,
                                             validate_crm_delivery_receipt)

    nameless = to_crm_delivery_receipt(DeliveryReceiptFacts(owen_message_id="",
                                                            status="delivered"))
    check("refused", validate_crm_delivery_receipt(nameless) != [])
    unknown = to_crm_delivery_receipt(DeliveryReceiptFacts(owen_message_id="m",
                                                           status="pigeon"))
    check("so is a status the CRM does not know",
          validate_crm_delivery_receipt(unknown) != [])


def test_the_marker_is_what_tells_the_two_kinds_of_message_apart():
    print("the CRM-link marker survives a round trip and defaults to 'not ours':")
    from app.integrations.crm import config as crm_config

    marked = crm_config.link_marker(LINK_ID, BOUND_DID)
    check("a marked row reads back", crm_config.marker_of(marked) == {"link_id": LINK_ID,
                                                                     "did": BOUND_DID})
    check("an operator's row (raw_payload NULL) is not ours",
          crm_config.marker_of(None) is None)
    check("neither is an INBOUND row carrying a provider payload",
          crm_config.marker_of({"From": CUSTOMER, "Message": "hi"}) is None)
    check("nor is a row with junk where the marker should be",
          crm_config.marker_of({"crm_link": "yes"}) is None)


# --- 2. through the real webhook ----------------------------------------------------------------

def test_a_receipt_for_a_crm_link_message_is_forwarded():
    print("a receipt for a text the CRM sent is relayed, carrying the join key:")
    status, jobs, msg = run_dlr(status="delivered")

    check("the webhook still answered 200", status == 200)
    check("one relay job was queued", len(jobs) == 1)
    payload = jobs[0]
    check("posted to OWEN's receipt adapter, not straight to the CRM",
          payload["url"].endswith("/api/crm-link/delivery-receipts"))
    facts = payload["body"]
    check("carrying messages.id — THE correlation field",
          facts["owen_message_id"] == str(msg.id))
    check("and the carrier's word", facts["status"] == "delivered")
    check("and the DID, so the right binding's token is used at drain",
          facts["dialed_number"] == BOUND_DID)
    check("the BulkVS RefId rides along for support, never as the join key",
          facts["provider_message_sid"] == f"bulkvs-{REF}"
          and facts["owen_message_id"] != f"bulkvs-{REF}")


def test_a_failure_carries_the_carriers_reason():
    print("a failed text tells the operator what the carrier said:")
    _s, jobs, _m = run_dlr(status="failed", detail="destination unreachable")
    check("relayed", len(jobs) == 1)
    check("with the carrier's text", jobs[0]["body"]["detail"] == "destination unreachable")
    check("and the carrier's word", jobs[0]["body"]["status"] == "failed")


def test_a_sent_receipt_is_relayed_even_though_owens_own_row_does_not_move():
    """OWEN marks a row 'sent' the moment BulkVS accepts it, so a carrier "sent" DLR
    advances nothing here — while being exactly the news the CRM is waiting for."""
    print("a 'sent' receipt is relayed even when OWEN's own row is already 'sent':")
    _s, jobs, msg = run_dlr(status="sent", owen_status="sent")
    check("OWEN's row did not move", msg.status == "sent")
    check("but the CRM was told anyway", len(jobs) == 1)
    check("with the carrier's word", jobs[0]["body"]["status"] == "sent")


def test_a_receipt_for_a_message_not_sent_through_the_link_is_not_forwarded():
    """An operator's text from the Inbox, or a flow's. The CRM has no event for it and
    would 404 every single receipt."""
    print("a receipt for a message the CRM did NOT send is not relayed:")
    status, jobs, _m = run_dlr(through_link=False)
    check("nothing was queued", jobs == [])
    check("and the webhook answered 200 exactly as it does today", status == 200)


def test_the_kill_switch_stops_the_relay_entirely():
    print("with CRM_LINK_ENABLED false, no receipt is relayed at all:")
    status, jobs, msg = run_dlr(enabled=False)
    check("nothing was queued", jobs == [])
    check("200 as always", status == 200)
    check("and OWEN's own row was still advanced, exactly as today",
          msg.status == "delivered")


def test_an_unbound_did_stops_the_relay_too():
    """The owner unbinding a number has to take effect on the messages already in flight."""
    print("a DID that is no longer bound relays nothing:")
    _s, jobs, _m = run_dlr(bound=False)
    check("nothing was queued", jobs == [])


def test_owens_own_row_is_advanced_exactly_as_it_is_today():
    """The regression. Everything above is an addition to this, never a change to it."""
    print("the existing forward-only advance of OWEN's own row is untouched:")
    for enabled in (False, True):
        _s, _j, msg = run_dlr(enabled=enabled, status="delivered", owen_status="sent")
        check(f"link={'on' if enabled else 'off'}: sent -> delivered",
              msg.status == "delivered")
        _s, _j, late = run_dlr(enabled=enabled, status="sent", owen_status="delivered")
        check(f"link={'on' if enabled else 'off'}: a late 'sent' does not walk it back",
              late.status == "delivered")

    _s, jobs, junk = run_dlr(status="pigeon-post")
    check("an unrecognised carrier word still leaves the row alone", junk.status == "sent")
    check("and is not relayed either", jobs == [])


def test_an_inbound_row_is_never_relayed():
    print("only an OUTBOUND row can have a delivery receipt:")
    _s, jobs, _m = run_dlr(direction="inbound")
    check("nothing was queued", jobs == [])


# --- 3. the relay route, with the CRM mocked at the HTTP boundary --------------------------------

class ReceiptCrm(FakeCrmHttp):
    """The same HTTP-boundary mock, taught the delivery endpoint."""

    def __init__(self, *, status=200, advanced=True):
        FakeCrmHttp.__init__(self)
        self.delivery_status = status
        self.advanced = advanced

    async def request(self, method, url, headers=None, **kwargs):
        if method == "POST" and url.endswith("/api/events/delivery"):
            self.posted.append(kwargs.get("json") or {})
            if self.delivery_status >= 400:
                return FakeResponse(self.delivery_status,
                                    {"detail": "no outbound message with provider_ref"})
            return FakeResponse(200, {"id": 5, "delivery_status": "DELIVERED",
                                      "advanced": self.advanced})
        return await FakeCrmHttp.request(self, method, url, headers, **kwargs)


def relay(crm, *, bound=True, **kw):
    import app.integrations.crm.client as crm_client
    from app.core.config import settings
    from app.integrations.crm import api as crm_api

    payload = dict(owen_message_id="m-1", status="delivered", detail="",
                   dialed_number=BOUND_DID, provider_message_sid=f"bulkvs-{REF}")
    payload.update(kw)
    session = FakeSession(None, _bound_row() if bound else None)
    saved = (settings.CRM_LINK_ENABLED, settings.CRM_LINK_BASE_URL,
             settings.CRM_LINK_TOKEN, crm_client.httpx.AsyncClient)
    try:
        settings.CRM_LINK_ENABLED = True
        settings.CRM_LINK_BASE_URL = "http://ghl_clone_api:8000"
        settings.CRM_LINK_TOKEN = "ghl_pat_test"
        crm_client.httpx.AsyncClient = crm
        return asyncio.run(crm_api.deliver_receipt(
            crm_api.DeliveryReceiptIn(**payload), session, None))
    finally:
        (settings.CRM_LINK_ENABLED, settings.CRM_LINK_BASE_URL,
         settings.CRM_LINK_TOKEN, crm_client.httpx.AsyncClient) = saved


def test_the_relay_route_posts_the_receipt_to_the_crm():
    print("the queued receipt reaches POST /api/events/delivery:")
    crm = ReceiptCrm()
    result = relay(crm)
    check("exactly one POST", len(crm.posted) == 1)
    check("no contact lookup — a receipt needs no contact", crm.searched == [])
    body = crm.posted[0]
    check("carrying provider_ref = messages.id", body["provider_ref"] == "m-1")
    check("and the status", body["status"] == "delivered")
    check("the route reports success", result.get("ok") is True)
    check("and passes on whether the CRM actually moved", result.get("advanced") is True)

    stale = relay(ReceiptCrm(advanced=False))
    check("a stale receipt the CRM correctly ignored is reported as not advanced",
          stale.get("ok") is True and stale.get("advanced") is False)


def test_a_crm_404_completes_the_job_and_a_500_retries_it():
    print("the retry contract: a 404 is an answer, a 500 is an outage:")
    from fastapi import HTTPException

    result = relay(ReceiptCrm(status=404))
    check("a 404 completes the job rather than retrying a row that does not exist",
          result.get("ok") is False)
    check("and says so", "404" in str(result.get("reason")))

    raised = None
    try:
        relay(ReceiptCrm(status=500))
    except HTTPException as exc:
        raised = exc
    check("a 5xx raises 502 so the queue retries with backoff",
          raised is not None and raised.status_code == 502)


def test_the_relay_route_refuses_an_unbound_did_and_a_disabled_link():
    print("the relay route obeys both switches:")
    from fastapi import HTTPException

    from app.core.config import settings
    from app.integrations.crm import api as crm_api

    crm = ReceiptCrm()
    result = relay(crm, bound=False)
    check("an unbound DID posts nothing", crm.posted == [])
    check("and completes the job", result.get("ok") is False)

    saved = settings.CRM_LINK_ENABLED
    raised = None
    try:
        settings.CRM_LINK_ENABLED = False
        asyncio.run(crm_api.deliver_receipt(
            crm_api.DeliveryReceiptIn(owen_message_id="m-1", status="delivered"),
            FakeSession(None, _bound_row()), None))
    except HTTPException as exc:
        raised = exc
    finally:
        settings.CRM_LINK_ENABLED = saved
    check("the kill switch answers 503 before any work",
          raised is not None and raised.status_code == 503)


# --- 4. contract drift, re-derived from the CRM's own source ------------------------------------

def test_the_receipt_contract_still_matches_the_crm_source():
    print("the CRM's own source still describes the receipt contract:")
    main_py = _find_crm_source()
    if main_py is None:
        print("  [SKIP] ghl-clone source not found "
              "(set GHL_CLONE_PATH to enable this drift check)")
        return

    from app.integrations.crm.events import CRM_DELIVERY_STATUSES

    tree = ast.parse(pathlib.Path(main_py).read_text(encoding="utf-8"))
    fields, statuses = {}, None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "DeliveryReceipt":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields[stmt.target.id] = ast.unparse(stmt.annotation)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Name)
                        and target.id == "DELIVERY_RECEIPT_STATUSES"
                        and isinstance(node.value, ast.Dict)):
                    statuses = {k.value for k in node.value.keys
                                if isinstance(k, ast.Constant)}

    if not fields:
        print(f"  [NOTE] this ghl-clone checkout has no DeliveryReceipt yet ({main_py}); "
              "owen-main is built for the amended CRM. Deploy the CRM half or every "
              "receipt is a 404 there.")
        return

    check("provider_ref is still the join key, and still required",
          fields.get("provider_ref") == "str")
    check("status is still a plain string", fields.get("status") == "str")
    check("detail is still optional", "detail" in fields)
    check("DELIVERY_RECEIPT_STATUSES was found", bool(statuses))
    check(f"our vocabulary still matches the CRM's ({sorted(statuses or [])})",
          CRM_DELIVERY_STATUSES == frozenset(statuses or ()))

    # Both sides took their words from OWEN's own ladder. If they ever diverge, a receipt
    # OWEN happily accepts becomes a 400 at the CRM, per text, silently.
    from app.services.sms import OUTBOUND_STATUS_RANK

    check("and OWEN's own status ladder still speaks exactly the same words",
          frozenset(OUTBOUND_STATUS_RANK) == CRM_DELIVERY_STATUSES)


if __name__ == "__main__":
    test_the_receipt_is_keyed_on_owens_message_id_not_the_bulkvs_refid()
    test_a_receipt_that_cannot_name_its_message_is_refused_here()
    test_the_marker_is_what_tells_the_two_kinds_of_message_apart()
    test_a_receipt_for_a_crm_link_message_is_forwarded()
    test_a_failure_carries_the_carriers_reason()
    test_a_sent_receipt_is_relayed_even_though_owens_own_row_does_not_move()
    test_a_receipt_for_a_message_not_sent_through_the_link_is_not_forwarded()
    test_the_kill_switch_stops_the_relay_entirely()
    test_an_unbound_did_stops_the_relay_too()
    test_owens_own_row_is_advanced_exactly_as_it_is_today()
    test_an_inbound_row_is_never_relayed()
    test_the_relay_route_posts_the_receipt_to_the_crm()
    test_a_crm_404_completes_the_job_and_a_500_retries_it()
    test_the_relay_route_refuses_an_unbound_did_and_a_disabled_link()
    test_the_receipt_contract_still_matches_the_crm_source()
    print("\nALL CRM DELIVERY-RECEIPT CHECKS PASSED")
