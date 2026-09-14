"""AHS work-order emails reach the CRM as well as GoHighLevel — and nothing about GHL changes.

The owner's decisions (2026-09-14): keep relaying to GoHighLevel exactly as today AND deliver
to the CRM; the two are independent; only emails that arrive after the switch is on; the
switch is off by default. Each test below drives the real poller, handler or adapter with the
database, the mailbox and the HTTP boundary faked, and asserts on what was queued, posted and
recorded — never on a return code alone.

No network, no database. The CRM contract check reads the ghl-clone source when it can find
it (GHL_CLONE_PATH, or a sibling checkout) and says SKIP when it cannot.

Run: python -m tests.test_crm_email_jobs
"""

import ast
import asyncio
import os
import pathlib
import uuid
from datetime import datetime, timezone

from tests.test_dispatch_email import PLAIN_BODY, SUBJECT

SENDER = "American Home Shield <notifications@dispatch.me>"
CANCEL_SUBJECT = "AHS Canceled Job 66450639"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"crm_email_jobs failed at: {name}")


# --- fakes ------------------------------------------------------------------------------------

class FakeEmail:
    """An `inbound_emails` row: every column the handlers read or write."""

    def __init__(self, *, subject=SUBJECT, text=PLAIN_BODY, crm_status=None):
        from app.providers import dispatch_email

        parsed = dispatch_email.parse(subject, text, None)
        self.id = uuid.uuid4()
        self.message_id = f"<{self.id}@dispatch.me>"
        self.source = dispatch_email.SOURCE
        self.from_addr = SENDER
        self.to_addr = "jobs@dreamteamroofingfl.com"
        self.subject = subject
        self.job_id = parsed.job_id
        self.parse_status = parsed.status
        self.parse_error = parsed.error
        self.fields = parsed.fields or None
        self.raw = "raw"
        self.relayed_to_ghl = False
        self.relayed_at = None
        self.relay_status = None
        self.relay_error = None
        self.relay_result = None
        self.received_at = datetime(2026, 9, 14, 13, 5, tzinfo=timezone.utc)
        self.crm_status = crm_status
        self.crm_error = None
        self.crm_result = None
        self.crm_attempted_at = None

    def ghl_state(self):
        return (self.relayed_to_ghl, self.relayed_at, self.relay_status, self.relay_error,
                self.relay_result)

    def crm_state(self):
        return (self.crm_status, self.crm_error, self.crm_result, self.crm_attempted_at)


class FakeDB:
    def __init__(self, row=None):
        self.row = row
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, _model, _pk):
        return self.row

    async def execute(self, _stmt):
        raise AssertionError("no query expected on this path")

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def refresh(self, _obj):
        return None


class Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = str(data)

    def json(self):
        return self._data


class FakeCrm:
    """`httpx.AsyncClient` as the CRM: records every POST, answers what it is told."""

    def __init__(self, status=201, data=None):
        self.status = status
        self.data = data if data is not None else {
            "outcome": "created", "ahs_job_id": "66450639",
            "opportunity": {"id": 41, "title": "66450639 ROOF - Guillermo Escala"},
            "contact": {"id": 7, "matched_by": "created"}, "note_id": 3}
        self.posted = []

    def __call__(self, *_a, **_kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def request(self, method, url, headers=None, **kwargs):
        self.posted.append((method, url, kwargs.get("json"), headers))
        return Resp(self.status, self.data)

    async def post(self, url, json=None, headers=None):
        return await self.request("POST", url, headers=headers, json=json)


SWITCHES = ("CRM_LINK_EMAIL_JOBS_ENABLED", "CRM_LINK_ENABLED", "CRM_LINK_TOKEN",
            "CRM_LINK_BASE_URL", "AGENT_RUNTIME_KEY", "OWEN_INTERNAL_URL",
            "INBOUND_MAIL_HOST", "INBOUND_MAIL_USER", "INBOUND_MAIL_MARK_SEEN",
            "GHL_EMAIL_WEBHOOK_URL")


class switched:
    """Set the settings a test needs and put every one of them back afterwards."""

    def __init__(self, on=True, **extra):
        self.values = {
            "CRM_LINK_EMAIL_JOBS_ENABLED": on, "CRM_LINK_ENABLED": True,
            "CRM_LINK_TOKEN": "ghl_pat_test", "CRM_LINK_BASE_URL": "http://ghl_clone_api:8000",
            "AGENT_RUNTIME_KEY": "owen_key_test", "OWEN_INTERNAL_URL": "http://app:8888",
            "INBOUND_MAIL_HOST": "imap.test", "INBOUND_MAIL_USER": "jobs@test",
            "INBOUND_MAIL_MARK_SEEN": True, **extra}

    def __enter__(self):
        from app.core.config import settings

        self.saved = {k: getattr(settings, k) for k in SWITCHES}
        for k, v in self.values.items():
            setattr(settings, k, v)
        return settings

    def __exit__(self, *_exc):
        from app.core.config import settings

        for k, v in self.saved.items():
            setattr(settings, k, v)
        return False


# --- the pure pieces ----------------------------------------------------------------------------

def test_the_total_becomes_integer_cents_without_a_float():
    print("payment total -> cents:")
    from app.integrations.crm.email_jobs import to_cents

    for raw, want in (("125", 12500), ("1,234.56", 123456), ("0.29", 29), ("$89.1", 8910),
                      (None, 0), ("", 0), ("n/a", 0), ("-5", 0), ("NaN", 0)):
        got = to_cents(raw)
        check(f"{raw!r} -> {want}", got == want and isinstance(got, int))


def test_the_crm_body_is_the_parsed_email_and_the_same_note_ghl_gets():
    print("the body posted to POST /api/ahs-jobs:")
    from app.integrations.crm.email_jobs import cancellation_body, job_body
    from app.services import emails

    em = FakeEmail()
    body = job_body(em)
    check("job id", body["ahs_job_id"] == "66450639")
    check("service", body["service"] == "ROOF")
    check("customer", body["customer_name"] == "Guillermo Escala")
    check("phone", body["phone"] == "+13059629757")
    check("address", body["service_address"] == "14436 SW 95TH LN MIAMI, FL 33186")
    check("value in cents", body["value_cents"] == 12500)
    check("the note is GHL's job_description, verbatim",
          body["description"] == emails.ghl_payload(em)["job_description"])
    ghl_name = emails.build_opportunity_body(em.fields, "c", "p", "s", "l")["name"]
    check(f"the CRM's title rule gives GHL's card name ({ghl_name!r})",
          ghl_name == f"{body['ahs_job_id']} {body['service']} - {body['customer_name']}")
    check("received_at is aware ISO", body["received_at"] == "2026-09-14T13:05:00+00:00")

    cancel = cancellation_body(FakeEmail(subject=CANCEL_SUBJECT, text=""))
    check("a cancellation names only the cancelled job",
          cancel["ahs_job_id"] == "66450639" and set(cancel) == {
              "ahs_job_id", "message_id", "received_at"})


# --- the poller ---------------------------------------------------------------------------------

class Msg:
    def __init__(self, uid, subject, text):
        self.uid = uid
        self.message_id = f"<{uid.decode()}@dispatch.me>"
        self.from_addr = SENDER
        self.subject = subject
        self.text_body = text
        self.html_body = None


def run_poll(*, on, old_message_ids=(), crm_enqueue_raises=False):
    """Drive `poll_mailbox` over four emails. Returns (queued, marked_seen, rows)."""
    from app.services import emails, mailbox, queue
    from app.workers import mail_poller

    msgs = [Msg(b"1", SUBJECT, PLAIN_BODY),                  # a new work order
            Msg(b"2", CANCEL_SUBJECT, ""),                     # a new cancellation
            Msg(b"3", "Welcome to Dispatch", "hello"),         # ignored
            Msg(b"4", SUBJECT, PLAIN_BODY)]                    # re-delivered, already stored
    rows, queued, seen = {}, [], []
    db = FakeDB()

    async def fake_ingest(_db, msg, parsed, _source):
        row = FakeEmail(subject=msg.subject, text=msg.text_body)
        row.message_id = msg.message_id
        rows[msg.message_id] = row
        return row, msg.message_id not in old_message_ids and msg.uid != b"4"

    async def fake_enqueue(_db, job_type, payload, delay_seconds=0):
        if crm_enqueue_raises and job_type == "email_relay_crm":
            raise RuntimeError("database hiccup")
        queued.append((job_type, payload["email_id"]))

    saved = (mailbox.fetch_from_sender, mailbox.mark_seen, emails.ingest_email,
             queue.enqueue, mail_poller.SessionLocal)
    try:
        mailbox.fetch_from_sender = lambda *a: msgs
        mailbox.mark_seen = lambda *a: seen.extend(a[-1])
        emails.ingest_email = fake_ingest
        queue.enqueue = fake_enqueue
        mail_poller.SessionLocal = lambda: db
        with switched(on=on):
            asyncio.run(mail_poller.poll_mailbox())
    finally:
        (mailbox.fetch_from_sender, mailbox.mark_seen, emails.ingest_email,
         queue.enqueue, mail_poller.SessionLocal) = saved
    by_uid = {m.uid: rows[m.message_id] for m in msgs}
    return queued, seen, by_uid


def test_switched_off_the_poller_queues_exactly_what_it_did_before():
    print("switch OFF (the default): only the GHL relay is queued:")
    queued, seen, rows = run_poll(on=False)
    check("the GHL relay jobs are exactly the work order and the cancellation",
          queued == [("email_relay_ghl", str(rows[b"1"].id)),
                     ("email_relay_ghl", str(rows[b"2"].id))])
    check("no row is stamped for the CRM", all(r.crm_status is None for r in rows.values()))
    check("every message is still marked seen", seen == [b"1", b"2", b"3", b"4"])


def test_switched_on_a_new_email_gets_its_own_crm_job_after_the_ghl_one():
    print("switch ON: a second, separate job per new work order or cancellation:")
    queued, seen, rows = run_poll(on=True)
    check("GHL first, then CRM, for each email; nothing for ignored or re-delivered mail",
          queued == [("email_relay_ghl", str(rows[b"1"].id)),
                     ("email_relay_crm", str(rows[b"1"].id)),
                     ("email_relay_ghl", str(rows[b"2"].id)),
                     ("email_relay_crm", str(rows[b"2"].id))])
    check("the two queued rows are stamped 'queued'",
          (rows[b"1"].crm_status, rows[b"2"].crm_status) == ("queued", "queued"))
    check("an ignored email and an already-stored one are never stamped",
          (rows[b"3"].crm_status, rows[b"4"].crm_status) == (None, None))
    check("the GHL relay state is untouched by queuing",
          rows[b"1"].ghl_state() == (False, None, None, None, None))
    check("every message is marked seen", seen == [b"1", b"2", b"3", b"4"])


def test_a_crm_queue_failure_costs_the_ghl_relay_nothing():
    print("the CRM enqueue blows up: GHL still queued, mail still marked seen:")
    queued, seen, rows = run_poll(on=True, crm_enqueue_raises=True)
    check("both GHL relay jobs are queued",
          queued == [("email_relay_ghl", str(rows[b"1"].id)),
                     ("email_relay_ghl", str(rows[b"2"].id))])
    check("every message is still marked seen", seen == [b"1", b"2", b"3", b"4"])


def test_the_crm_job_payload_carries_no_customer_data():
    print("the jobs table holds an id, not a customer:")
    from app.integrations.crm import email_jobs
    from app.services import queue

    row, db, got = FakeEmail(), FakeDB(), []

    async def fake_enqueue(_db, job_type, payload, delay_seconds=0):
        got.append((job_type, payload))

    saved = queue.enqueue
    try:
        queue.enqueue = fake_enqueue
        with switched(on=True):
            asyncio.run(email_jobs.enqueue_for_new_email(db, row, "parsed", created=True))
    finally:
        queue.enqueue = saved
    check("one job, payload is only the email id",
          got == [("email_relay_crm", {"email_id": str(row.id)})])


def test_every_switch_is_needed():
    print("the CRM job needs its own switch, the link switch, a token and the runtime key:")
    from app.integrations.crm.email_jobs import refusal

    with switched(on=True) as s:
        check("all set -> allowed", refusal(s) is None)
    for name, value in (("CRM_LINK_EMAIL_JOBS_ENABLED", False), ("CRM_LINK_ENABLED", False),
                        ("CRM_LINK_TOKEN", ""), ("AGENT_RUNTIME_KEY", "")):
        with switched(on=True, **{name: value}) as s:
            check(f"{name}={value!r} -> refused", refusal(s) is not None)
    from app.core.config import Settings

    check("CRM_LINK_EMAIL_JOBS_ENABLED defaults to False",
          Settings.model_fields["CRM_LINK_EMAIL_JOBS_ENABLED"].default is False)


# --- the worker handler -------------------------------------------------------------------------

def run_handler(row, *, on=True, adapter_status=200):
    from app.workers import handlers

    adapter = FakeCrm(status=adapter_status, data={"ok": True})
    db = FakeDB(row)
    saved = handlers.httpx.AsyncClient
    raised = None
    try:
        handlers.httpx.AsyncClient = adapter
        with switched(on=on):
            try:
                asyncio.run(handlers.handle_email_relay_crm(db, {"email_id": str(row.id)}))
            except RuntimeError as exc:
                raised = exc
    finally:
        handlers.httpx.AsyncClient = saved
    return adapter, raised


def test_an_email_from_before_the_switch_is_never_sent():
    print("NO BACKFILL: a row the poller never stamped is refused by the job:")
    old = FakeEmail(crm_status=None)
    old.relayed_to_ghl, old.relay_status = True, "sent"
    before = (old.ghl_state(), old.crm_state())
    adapter, raised = run_handler(old)
    check("nothing is posted", adapter.posted == [])
    check("nothing raised, nothing recorded", raised is None
          and (old.ghl_state(), old.crm_state()) == before)
    done = FakeEmail(crm_status="sent")
    adapter, _ = run_handler(done)
    check("a finished delivery is not sent twice", adapter.posted == [])


def test_switched_off_after_queuing_nothing_is_sent_and_it_says_so():
    print("a job already queued when the switch goes off:")
    row = FakeEmail(crm_status="queued")
    adapter, raised = run_handler(row, on=False)
    check("nothing is posted", adapter.posted == [] and raised is None)
    check("recorded as skipped_disabled with the reason",
          row.crm_status == "skipped_disabled" and "CRM_LINK_EMAIL_JOBS_ENABLED" in row.crm_error)


def test_the_handler_hands_the_id_to_the_app_adapter_and_retries_on_failure():
    print("the worker's hop:")
    row = FakeEmail(crm_status="queued")
    adapter, raised = run_handler(row)
    method, url, body, headers = adapter.posted[0]
    check("posts to OWEN's own adapter", url == "http://app:8888/api/crm-link/email-jobs")
    check("with the email id only, and the runtime key",
          body == {"email_id": str(row.id)} and headers == {"X-OWEN-Key": "owen_key_test"})
    check("200 completes the job", raised is None)

    row = FakeEmail(crm_status="queued")
    ghl_before = row.ghl_state()
    _adapter, raised = run_handler(row, adapter_status=502)
    check("a 502 raises so the queue retries", raised is not None)
    check("and records 'failed'", row.crm_status == "failed" and "502" in row.crm_error)
    check("the GHL relay columns are untouched", row.ghl_state() == ghl_before)


# --- the app-side adapter -----------------------------------------------------------------------

def run_adapter(row, crm, *, on=True):
    from fastapi import HTTPException

    import app.integrations.crm.client as crm_client
    from app.integrations.crm import api as crm_api

    db = FakeDB(row)
    saved = crm_client.httpx.AsyncClient
    try:
        crm_client.httpx.AsyncClient = crm
        with switched(on=on):
            try:
                out = asyncio.run(crm_api.deliver_email_job(
                    crm_api.EmailJobDeliveryIn(email_id=str(row.id)), db, None))
            except HTTPException as exc:
                out = exc
    finally:
        crm_client.httpx.AsyncClient = saved
    return out


def test_the_adapter_posts_the_work_order_and_records_the_card():
    print("the adapter delivers a work order and records what the CRM made:")
    from app.integrations.crm.email_jobs import job_body

    row = FakeEmail(crm_status="queued")
    ghl_before = row.ghl_state()
    crm = FakeCrm()
    out = run_adapter(row, crm)
    method, url, body, headers = crm.posted[0]
    check("POST /api/ahs-jobs on the CRM", (method, url) == (
        "POST", "http://ghl_clone_api:8000/api/ahs-jobs"))
    check("with the CRM link's token", headers["Authorization"] == "Bearer ghl_pat_test")
    check("the body is the parsed email", body == job_body(row))
    check("recorded 'sent'", row.crm_status == "sent" and out["ok"] is True)
    check("the result keeps ids, never the customer",
          row.crm_result == {"outcome": "created", "ahs_job_id": "66450639",
                             "opportunity_id": 41, "contact_id": 7,
                             "matched_by": "created", "note_id": 3})
    check("the GHL relay columns are untouched", row.ghl_state() == ghl_before)

    again = FakeEmail(crm_status="failed")
    run_adapter(again, FakeCrm(status=200, data={"outcome": "existing",
                                                 "opportunity": {"id": 41},
                                                 "contact": {"id": 7}}))
    check("a repeat delivery the CRM already had is 'existing'", again.crm_status == "existing")


def test_the_adapter_retries_a_down_crm_and_does_not_retry_a_refusal():
    print("CRM down vs CRM refusing:")
    from fastapi import HTTPException

    row = FakeEmail(crm_status="queued")
    out = run_adapter(row, FakeCrm(status=500, data={"detail": "boom"}))
    check("a 5xx is a 502 (the queue retries)",
          isinstance(out, HTTPException) and out.status_code == 502)
    check("recorded 'failed'", row.crm_status == "failed" and "500" in row.crm_error)

    row = FakeEmail(crm_status="queued")
    out = run_adapter(row, FakeCrm(status=422, data={"detail": "pipeline not found"}))
    check("a 4xx completes the job (no retry fixes it)", out["ok"] is False)
    check("recorded 'refused' with the CRM's reason",
          row.crm_status == "refused" and "pipeline not found" in row.crm_error)


def test_the_adapter_delivers_a_cancellation_and_a_no_card_is_recorded():
    print("cancellations:")
    row = FakeEmail(subject=CANCEL_SUBJECT, text="", crm_status="queued")
    crm = FakeCrm(status=200, data={"outcome": "noted", "ahs_job_id": "66450639",
                                    "opportunity_id": 41, "note_id": 9})
    run_adapter(row, crm)
    check("POST /api/ahs-jobs/cancellations",
          crm.posted[0][1].endswith("/api/ahs-jobs/cancellations"))
    check("recorded 'cancellation_noted'", row.crm_status == "cancellation_noted"
          and row.crm_result["opportunity_id"] == 41)

    row = FakeEmail(subject=CANCEL_SUBJECT, text="", crm_status="queued")
    run_adapter(row, FakeCrm(status=200, data={"outcome": "no_card",
                                               "ahs_job_id": "66450639"}))
    check("no card for the job is a recorded no-op", row.crm_status == "skipped_no_card")


def test_the_adapter_refuses_an_unstamped_row_and_a_closed_switch():
    print("the adapter repeats the guards:")
    from fastapi import HTTPException

    old = FakeEmail(crm_status=None)
    crm = FakeCrm()
    out = run_adapter(old, crm)
    check("an unstamped (pre-switch) row is skipped, nothing posted",
          out.get("skipped") is True and crm.posted == [] and old.crm_status is None)
    row = FakeEmail(crm_status="queued")
    crm = FakeCrm()
    out = run_adapter(row, crm, on=False)
    check("switched off -> 503, nothing posted, nothing recorded",
          isinstance(out, HTTPException) and out.status_code == 503 and crm.posted == []
          and row.crm_status == "queued")


# --- the GHL relay is unchanged -----------------------------------------------------------------

def test_the_ghl_relay_does_not_know_the_crm_exists():
    print("the GHL relay, with the CRM failing beside it:")
    import inspect

    from app.workers import handlers

    for fn in (handlers.handle_email_relay_ghl, handlers._relay_cancellation,
               handlers._relay_via_api):
        check(f"{fn.__name__} never mentions the CRM", "crm" not in inspect.getsource(fn).lower())

    # The CRM delivery for this email has failed. The GHL relay runs exactly as it would.
    row = FakeEmail(crm_status="failed")
    row.crm_error = "CRM 500: boom"
    crm_before = row.crm_state()
    sent = []

    async def fake_webhook(payload):
        sent.append(payload)

    saved = (handlers.ghl_client.post_inbound_email,)
    try:
        handlers.ghl_client.post_inbound_email = fake_webhook
        with switched(on=True, GHL_EMAIL_WEBHOOK_URL="https://ghl.test/hook"):
            from app.core.config import settings
            api_on = settings.ghl_api_enabled
            if not api_on:
                asyncio.run(handlers.handle_email_relay_ghl(FakeDB(row),
                                                            {"email_id": str(row.id)}))
    finally:
        (handlers.ghl_client.post_inbound_email,) = saved
    if api_on:
        print("  [SKIP] GHL API credentials are configured in this environment")
        return
    check("GHL received the payload once", len(sent) == 1 and sent[0]["job_id"] == "66450639")
    check("recorded as sent to GHL", row.relayed_to_ghl is True and row.relay_status == "sent")
    check("the CRM columns are untouched", row.crm_state() == crm_before)


# --- the contract with the CRM's source ---------------------------------------------------------

def _crm_base():
    env = os.environ.get("GHL_CLONE_PATH")
    here = pathlib.Path(__file__).resolve()
    for base in ([pathlib.Path(env)] if env else []) + [
            here.parents[2].parent / "ghl-clone", pathlib.Path.home() / "ghl-clone"]:
        if (base / "backend" / "app" / "ahs_jobs.py").is_file():
            return base
    return None


def test_the_body_still_matches_the_crm_source():
    print("the CRM's own source still describes POST /api/ahs-jobs:")
    base = _crm_base()
    if base is None:
        print("  [SKIP] a ghl-clone checkout with app/ahs_jobs.py was not found "
              "(set GHL_CLONE_PATH to enable this drift check)")
        return
    from app.integrations.crm.email_jobs import cancellation_body, job_body

    tree = ast.parse((base / "backend" / "app" / "ahs_jobs.py").read_text(encoding="utf-8"))
    models = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in ("AhsJobIn", "AhsCancellationIn"):
            models[node.name] = {s.target.id for s in node.body
                                 if isinstance(s, ast.AnnAssign)
                                 and isinstance(s.target, ast.Name)}
    check("every key we send is a field the CRM reads (work order)",
          set(job_body(FakeEmail())) <= models.get("AhsJobIn", set()))
    check("every key we send is a field the CRM reads (cancellation)",
          set(cancellation_body(FakeEmail(subject=CANCEL_SUBJECT, text="")))
          <= models.get("AhsCancellationIn", set()))
    main = (base / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    auth = (base / "backend" / "app" / "auth.py").read_text(encoding="utf-8")
    check("both routes exist", '"/api/ahs-jobs"' in main
          and '"/api/ahs-jobs/cancellations"' in main)
    check("an events:write token may reach both", '"/api/ahs-jobs"' in auth
          and '"/api/ahs-jobs/cancellations"' in auth)


if __name__ == "__main__":
    test_the_total_becomes_integer_cents_without_a_float()
    test_the_crm_body_is_the_parsed_email_and_the_same_note_ghl_gets()
    test_switched_off_the_poller_queues_exactly_what_it_did_before()
    test_switched_on_a_new_email_gets_its_own_crm_job_after_the_ghl_one()
    test_a_crm_queue_failure_costs_the_ghl_relay_nothing()
    test_the_crm_job_payload_carries_no_customer_data()
    test_every_switch_is_needed()
    test_an_email_from_before_the_switch_is_never_sent()
    test_switched_off_after_queuing_nothing_is_sent_and_it_says_so()
    test_the_handler_hands_the_id_to_the_app_adapter_and_retries_on_failure()
    test_the_adapter_posts_the_work_order_and_records_the_card()
    test_the_adapter_retries_a_down_crm_and_does_not_retry_a_refusal()
    test_the_adapter_delivers_a_cancellation_and_a_no_card_is_recorded()
    test_the_adapter_refuses_an_unstamped_row_and_a_closed_switch()
    test_the_ghl_relay_does_not_know_the_crm_exists()
    test_the_body_still_matches_the_crm_source()
    print("\nALL CRM EMAIL-JOB CHECKS PASSED")
