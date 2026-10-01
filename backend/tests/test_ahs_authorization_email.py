"""An AHS note that approves a repair reaches the CRM — behind its own switch, off by default.

2026-10-01. Of 31 Dispatch note emails ("American Home Shield sent you a note for job #<n>")
exactly ONE was an authorization, with this body (digits redacted when it was read):

    Note Added in Frontdoor System NCC $#### Net Total $#### AUTHO # ####RNCL Thanks for
    being the best!

Every sample below is built from that REDACTED template with INVENTED digits. No real
customer, job or authorization number appears in this file. Four other notes said "Your list
of items to service have been updated", which MAY mean an approval and is its own weaker kind.

No network, no database: the poller, handler and adapter run with the mailbox, the queue, the
database and the HTTP boundary faked (the fakes are `test_crm_email_jobs`'s).

Run: python -m tests.test_ahs_authorization_email
"""

import ast
import asyncio
import os
import pathlib

from tests.test_crm_email_jobs import (
    PLAIN_BODY,
    SUBJECT,
    FakeCrm,
    FakeDB,
    FakeEmail,
    Msg,
    switched,
)

NOTE_SUBJECT = "American Home Shield sent you a note for job #71234567"
# The real template, invented digits.
AUTH_BODY = ("Note Added in Frontdoor System NCC $1500 Net Total $1350 AUTHO # 4821RNCL "
             "Thanks for being the best!")
ITEMS_BODY = "Your list of items to service have been updated."


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"ahs_authorization_email failed at: {name}")


class auth_switch:
    """CRM_LINK_AHS_AUTHORIZATIONS_ENABLED, put back afterwards."""

    def __init__(self, on):
        self.on = on

    def __enter__(self):
        from app.core.config import settings

        self.saved = getattr(settings, "CRM_LINK_AHS_AUTHORIZATIONS_ENABLED")
        settings.CRM_LINK_AHS_AUTHORIZATIONS_ENABLED = self.on
        return settings

    def __exit__(self, *_exc):
        from app.core.config import settings

        settings.CRM_LINK_AHS_AUTHORIZATIONS_ENABLED = self.saved
        return False


# --- the parser ---------------------------------------------------------------------------

def test_the_template_is_an_authorization():
    print("the observed template, invented digits:")
    from app.providers import dispatch_email as d

    p = d.parse(NOTE_SUBJECT, AUTH_BODY, None)
    check("status 'authorization'", p.status == d.AUTHORIZATION == "authorization")
    check("never a GHL lead", p.ok is False)
    check("job number from the subject", p.job_id == "71234567"
          and p.fields["job_id"] == "71234567")
    check("AUTHO number is the digits", p.fields["autho_number"] == "4821")
    check("the letter code stuck to it is kept apart", p.fields["autho_code"] == "RNCL")
    check("net total", p.fields["net_total"] == "1350")
    check("NCC", p.fields["ncc"] == "1500")
    check("amounts are strings, never floats",
          all(isinstance(p.fields[k], str) for k in ("net_total", "ncc")))
    check("the stored reason is a sentence", "authorized job 71234567" in p.error)


def test_spacing_dollar_signs_and_commas_do_not_matter():
    print("tolerant of spacing, $, commas, cents and HTML:")
    from app.providers import dispatch_email as d

    a = "Note Added in Frontdoor System "
    variants = {
        "no spaces, no $": "NoteAddedinFrontdoorSystem NCC1500 Net Total1350 AUTHO#4821RNCL Thanks",
        "commas and cents": a + "NCC $ 1,500.00   Net  Total:  $1,350.25  AUTHO  #  4821RNCL",
        "line breaks": "Note\nAdded\nin\nFrontdoor\nSystem\nNCC\n$1500\nNet\nTotal\n$1350"
                       "\nAUTHO\n#\n4821RNCL\nThanks",
        "lower case": "note added in frontdoor system ncc $1500 net total $1350 autho # 4821rncl "
                      "thanks",
        "html": "<p>Note Added in Frontdoor System</p><p>NCC <b>$1500</b></p>"
                "<p>Net Total&nbsp;&#36;1350</p><p>AUTHO &#35; 4821RNCL</p>",
        "space before the code": a + "NCC $1500 Net Total $1350 AUTHO # 4821 RNCL Thanks",
        "code glued to Thanks": a + "NCC $1500 Net Total $1350 AUTHO # 4821RNCLThanks for",
        "Authorization # spelled out": a + "Net Total $1350 Authorization # 4821",
    }
    for name, body in variants.items():
        p = d.parse(NOTE_SUBJECT, body, None)
        check(f"{name}: authorization", p.status == d.AUTHORIZATION)
        check(f"{name}: AUTHO 4821", p.fields.get("autho_number") == "4821")
    p = d.parse(NOTE_SUBJECT, variants["commas and cents"], None)
    check("commas stripped, cents kept", (p.fields["ncc"], p.fields["net_total"])
          == ("1500.00", "1350.25"))
    p = d.parse(NOTE_SUBJECT, variants["lower case"], None)
    check("'Thanks' / a lower-case tail is never read as a code", "autho_code" not in p.fields)
    p = d.parse(NOTE_SUBJECT, None, variants["html"])
    check("an HTML-only email is read too", p.status == d.AUTHORIZATION
          and p.fields["net_total"] == "1350")
    for name in ("html", "space before the code", "code glued to Thanks"):
        p = d.parse(NOTE_SUBJECT, variants[name], None)
        check(f"{name}: the code is RNCL", p.fields.get("autho_code") == "RNCL")
    p = d.parse(NOTE_SUBJECT, a + "NCC $1500 Net Total $1350 AUTHO # 4821 Thanks", None)
    check("'4821 Thanks': AUTHO 4821, no code", p.fields.get("autho_number") == "4821"
          and "autho_code" not in p.fields)
    p = d.parse(NOTE_SUBJECT, a + "NCC $1500 AUTHO # 48-21RNCL", None)
    check("the AUTHO number is digits only (it is half the idempotency key)",
          p.fields.get("autho_number") == "4821")
    p = d.parse(NOTE_SUBJECT, a + "NCC $1500 AUTHO # 4821RNCL", None)
    check("NCC alone is enough, net total absent",
          p.status == d.AUTHORIZATION and "net_total" not in p.fields)


def test_what_is_not_an_authorization():
    print("what must NOT read as an authorization:")
    from app.providers import dispatch_email as d

    check("an AUTHO number with no amount stays ignored",
          d.parse(NOTE_SUBJECT, "AUTHO # 4821RNCL", None).status == d.IGNORED)
    check("an amount with no AUTHO stays ignored",
          d.parse(NOTE_SUBJECT, "Net Total $1350", None).status == d.IGNORED)
    check("the work order's 'Authorization Link: Click Here' is not one",
          d.parse(NOTE_SUBJECT, "Authorization Link: Click Here", None).status == d.IGNORED)
    check("the request to send a report to the Authorization department is not one",
          d.parse(NOTE_SUBJECT, "Please send the report to the Authorization department.",
                  None).status == d.IGNORED)
    check("no anchor: AUTHO + amounts without 'Note Added in Frontdoor System' stay ignored",
          d.parse(NOTE_SUBJECT, "NCC $1500 Net Total $1350 AUTHO # 4821RNCL", None).status
          == d.IGNORED)
    check("'Authorization 4821 DENIED. Net Total $0' is not one",
          d.parse(NOTE_SUBJECT, "Authorization 4821 DENIED. Net Total $0", None).status
          == d.IGNORED)
    for word in ("DENIED", "declined", "Void", "voided", "cancelled", "canceled", "cancel",
                 "not authorized", "rejected", "revoked"):
        p = d.parse(NOTE_SUBJECT, "Note Added in Frontdoor System NCC $1500 Net Total $1350 "
                    f"AUTHO # 4821RNCL {word}. Thanks for being the best!", None)
        check(f"the template saying '{word}' is not one", p.status == d.IGNORED)
    check("a refusal word still falls back to 'possible' when items were updated",
          d.parse(NOTE_SUBJECT, ITEMS_BODY + " Note Added in Frontdoor System NCC $1500 "
                  "Net Total $1350 AUTHO # 4821RNCL DENIED", None).status
          == d.AUTHORIZATION_POSSIBLE)
    check("a note quoting an older note is not one",
          d.parse(NOTE_SUBJECT, "Customer called. Previous note: NCC $1500 Net Total $1350 "
                  "AUTHO # 4821RNCL", None).status == d.IGNORED)
    check("a note quoting the full older template is not one either",
          d.parse(NOTE_SUBJECT, "Customer called to reschedule. Previous note: " + AUTH_BODY,
                  None).status == d.IGNORED)
    check("an AUTHO before the anchor is not read",
          d.parse(NOTE_SUBJECT, "AUTHO # 4821RNCL Note Added in Frontdoor System NCC $1500",
                  None).status == d.IGNORED)
    check("the work order boilerplate 'Authorization Link: Click Here' next to a total",
          d.parse(NOTE_SUBJECT, "Note Added in Frontdoor System Authorization Link: Click Here "
                  "Net Total $1350", None).status == d.IGNORED)
    check("an empty note is still ignored",
          d.parse(NOTE_SUBJECT, "", "").status == d.IGNORED)
    check("the template under a non-note subject is not one (no job number to act on)",
          d.parse("Welcome to Dispatch", AUTH_BODY, None).status == d.IGNORED)
    check("a work order is still a parsed lead", d.parse(SUBJECT, PLAIN_BODY, None).status
          == d.PARSED)
    check("a cancellation is still a cancellation",
          d.parse("AHS Canceled Job 71234567", "", None).status == d.CANCELLATION)


def test_items_updated_is_a_possible_authorization():
    print("'items to service have been updated':")
    from app.providers import dispatch_email as d

    p = d.parse(NOTE_SUBJECT, ITEMS_BODY, None)
    check("status 'authorization_possible'", p.status == d.AUTHORIZATION_POSSIBLE)
    check("carries only the job", p.fields == {"source": "dispatch",
                                               "kind": "authorization_possible",
                                               "job_id": "71234567"})
    check("'has been updated' too",
          d.parse(NOTE_SUBJECT, "Your item to service has been updated", None).status
          == d.AUTHORIZATION_POSSIBLE)
    p = d.parse(NOTE_SUBJECT, ITEMS_BODY + " Previous note: " + AUTH_BODY, None)
    check("items updated that QUOTES an older AUTHO is 'possible', never a duplicate "
          "authorization", p.status == d.AUTHORIZATION_POSSIBLE and "autho_number" not in p.fields)
    check("items updated wins over an AUTHO in the same note (the safe direction)",
          d.parse(NOTE_SUBJECT, ITEMS_BODY + " " + AUTH_BODY, None).status
          == d.AUTHORIZATION_POSSIBLE)


# --- the CRM body -------------------------------------------------------------------------

def test_the_crm_body_and_its_dedupe_key():
    print("the body posted to POST /api/ahs-jobs/authorizations:")
    from app.integrations.crm import email_jobs

    em = FakeEmail(subject=NOTE_SUBJECT, text=AUTH_BODY)
    body = email_jobs.authorization_body(em)
    check("kind + job + AUTHO + amounts",
          {k: body[k] for k in ("kind", "ahs_job_id", "autho_number", "autho_code",
                                "net_total", "ncc")}
          == {"kind": "authorization", "ahs_job_id": "71234567", "autho_number": "4821",
              "autho_code": "RNCL", "net_total": "1350", "ncc": "1500"})
    check("idempotent per (job, AUTHO)", body["dedupe_key"] == "ahs_auth:71234567:4821")
    again = FakeEmail(subject=NOTE_SUBJECT, text=AUTH_BODY)
    check("the same approval in a second email has the same key",
          email_jobs.authorization_body(again)["dedupe_key"] == body["dedupe_key"]
          and again.message_id != em.message_id)

    em = FakeEmail(subject=NOTE_SUBJECT, text=ITEMS_BODY)
    body = email_jobs.authorization_body(em)
    check("a possible one carries no amounts", set(body) == {
        "kind", "ahs_job_id", "dedupe_key", "message_id", "received_at"})
    check("and is keyed per email", body["dedupe_key"]
          == f"ahs_auth_possible:71234567:{em.message_id}")


# --- the switch ---------------------------------------------------------------------------

def test_its_own_switch_off_by_default():
    print("CRM_LINK_AHS_AUTHORIZATIONS_ENABLED:")
    from app.core.config import Settings
    from app.integrations.crm.email_jobs import refusal

    check("defaults to False",
          Settings.model_fields["CRM_LINK_AHS_AUTHORIZATIONS_ENABLED"].default is False)
    with switched(on=True) as s, auth_switch(False):
        check("work orders on, authorizations off -> an authorization is refused",
              refusal(s, "authorization") is not None
              and "CRM_LINK_AHS_AUTHORIZATIONS_ENABLED" in refusal(s, "authorization"))
        check("...and a work order is still allowed", refusal(s, "parsed") is None)
    with switched(on=False) as s, auth_switch(True):
        check("authorizations on, work orders off -> an authorization is allowed",
              refusal(s, "authorization") is None
              and refusal(s, "authorization_possible") is None)
        check("...and a work order is still refused", refusal(s, "parsed") is not None)
    with switched(on=False, CRM_LINK_ENABLED=False) as s, auth_switch(True):
        check("the link itself off -> refused", refusal(s, "authorization") is not None)


# --- the poller ---------------------------------------------------------------------------

def run_poll(*, email_jobs_on, auth_on):
    from app.services import emails, mailbox, queue
    from app.workers import mail_poller

    msgs = [Msg(b"1", SUBJECT, PLAIN_BODY), Msg(b"2", NOTE_SUBJECT, AUTH_BODY),
            Msg(b"3", NOTE_SUBJECT, ITEMS_BODY), Msg(b"4", NOTE_SUBJECT, "Call the member")]
    rows, queued, seen = {}, [], []
    db = FakeDB()

    async def fake_ingest(_db, msg, parsed, _source):
        row = FakeEmail(subject=msg.subject, text=msg.text_body)
        row.message_id = msg.message_id
        rows[msg.uid] = row
        return row, True

    async def fake_enqueue(_db, job_type, payload, delay_seconds=0):
        queued.append((job_type, payload["email_id"]))

    saved = (mailbox.fetch_from_sender, mailbox.mark_seen, emails.ingest_email,
             queue.enqueue, mail_poller.SessionLocal)
    try:
        mailbox.fetch_from_sender = lambda *a: msgs
        mailbox.mark_seen = lambda *a: seen.extend(a[-1])
        emails.ingest_email = fake_ingest
        queue.enqueue = fake_enqueue
        mail_poller.SessionLocal = lambda: db
        with switched(on=email_jobs_on), auth_switch(auth_on):
            asyncio.run(mail_poller.poll_mailbox())
    finally:
        (mailbox.fetch_from_sender, mailbox.mark_seen, emails.ingest_email,
         queue.enqueue, mail_poller.SessionLocal) = saved
    return queued, seen, rows


def test_the_poller():
    print("the poller:")
    queued, seen, rows = run_poll(email_jobs_on=True, auth_on=False)
    check("switch OFF: the work order goes to GHL and the CRM, the notes go nowhere",
          queued == [("email_relay_ghl", str(rows[b"1"].id)),
                     ("email_relay_crm", str(rows[b"1"].id))])
    check("the notes are stored with their kinds",
          (rows[b"2"].parse_status, rows[b"3"].parse_status, rows[b"4"].parse_status)
          == ("authorization", "authorization_possible", "ignored"))
    check("and never stamped for the CRM",
          (rows[b"2"].crm_status, rows[b"3"].crm_status) == (None, None))
    check("every message is marked seen", seen == [b"1", b"2", b"3", b"4"])

    queued, _seen, rows = run_poll(email_jobs_on=False, auth_on=True)
    check("switch ON: one CRM job per authorization note, never a GHL relay for a note",
          queued == [("email_relay_ghl", str(rows[b"1"].id)),
                     ("email_relay_crm", str(rows[b"2"].id)),
                     ("email_relay_crm", str(rows[b"3"].id))])
    check("an ordinary note is not queued", rows[b"4"].crm_status is None)


# --- the worker handler and the app adapter -----------------------------------------------

def test_the_handler_skips_when_the_switch_is_off():
    print("a queued authorization when its switch goes off:")
    from app.workers import handlers

    row = FakeEmail(subject=NOTE_SUBJECT, text=AUTH_BODY, crm_status="queued")
    adapter = FakeCrm(status=200, data={"ok": True})
    saved = handlers.httpx.AsyncClient
    try:
        handlers.httpx.AsyncClient = adapter
        with switched(on=True), auth_switch(False):
            asyncio.run(handlers.handle_email_relay_crm(FakeDB(row), {"email_id": str(row.id)}))
    finally:
        handlers.httpx.AsyncClient = saved
    check("nothing posted", adapter.posted == [])
    check("skipped_disabled, naming the new switch", row.crm_status == "skipped_disabled"
          and "CRM_LINK_AHS_AUTHORIZATIONS_ENABLED" in row.crm_error)


def run_adapter(row, crm, *, auth_on=True):
    import app.integrations.crm.client as crm_client
    from app.integrations.crm import api as crm_api
    from fastapi import HTTPException

    saved = crm_client.httpx.AsyncClient
    try:
        crm_client.httpx.AsyncClient = crm
        with switched(on=False), auth_switch(auth_on):
            try:
                return asyncio.run(crm_api.deliver_email_job(
                    crm_api.EmailJobDeliveryIn(email_id=str(row.id)), FakeDB(row), None))
            except HTTPException as exc:
                return exc
    finally:
        crm_client.httpx.AsyncClient = saved


def test_the_adapter_posts_the_authorization():
    print("the adapter:")
    from app.integrations.crm import email_jobs
    from fastapi import HTTPException

    row = FakeEmail(subject=NOTE_SUBJECT, text=AUTH_BODY, crm_status="queued")
    crm = FakeCrm(status=201, data={"outcome": "created", "ahs_job_id": "71234567",
                                    "opportunity_id": 41})
    out = run_adapter(row, crm)
    method, url, body, headers = crm.posted[0]
    check("POST /api/ahs-jobs/authorizations on the CRM", (method, url) == (
        "POST", "http://ghl_clone_api:8000/api/ahs-jobs/authorizations"))
    check("with the link's token", headers["Authorization"] == "Bearer ghl_pat_test")
    check("the body is the authorization", body == email_jobs.authorization_body(row))
    check("recorded 'authorization_sent'", row.crm_status == "authorization_sent"
          and out["ok"] is True)

    row = FakeEmail(subject=NOTE_SUBJECT, text=AUTH_BODY, crm_status="queued")
    run_adapter(row, FakeCrm(status=200, data={"outcome": "existing"}))
    check("a repeat is 'authorization_existing'", row.crm_status == "authorization_existing")

    row = FakeEmail(subject=NOTE_SUBJECT, text=AUTH_BODY, crm_status="queued")
    crm = FakeCrm()
    out = run_adapter(row, crm, auth_on=False)
    check("its switch off -> 503, nothing posted, nothing recorded",
          isinstance(out, HTTPException) and out.status_code == 503 and crm.posted == []
          and row.crm_status == "queued")

    row = FakeEmail(subject=NOTE_SUBJECT, text=AUTH_BODY, crm_status="queued")
    out = run_adapter(row, FakeCrm(status=503, data={"detail": "down"}))
    check("a CRM that is down is a 502 the queue retries",
          isinstance(out, HTTPException) and out.status_code == 502
          and row.crm_status == "failed")


# --- the contract with the CRM's source ---------------------------------------------------

def _crm_module():
    env = os.environ.get("GHL_CLONE_PATH")
    here = pathlib.Path(__file__).resolve()
    sib = here.parents[2].parent
    for base in ([pathlib.Path(env)] if env else []) + [
            sib / "ghl-clone", pathlib.Path.home() / "ghl-clone"]:
        f = base / "backend" / "app" / "ahs_authorizations.py"
        if f.is_file():
            return f
    return None


def test_the_body_matches_the_crm_source():
    print("the CRM's own source describes POST /api/ahs-jobs/authorizations:")
    f = _crm_module()
    if f is None:
        print("  [SKIP] no ghl-clone checkout with app/ahs_authorizations.py "
              "(set GHL_CLONE_PATH to enable this drift check)")
        return
    from app.integrations.crm import email_jobs

    src = f.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fields = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "AhsAuthorizationIn":
            fields = {s.target.id for s in node.body
                      if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)}
    for text in (AUTH_BODY, ITEMS_BODY):
        sent = set(email_jobs.authorization_body(FakeEmail(subject=NOTE_SUBJECT, text=text)))
        check(f"every key we send is a field the CRM reads ({sorted(sent - fields) or 'ok'})",
              fields and sent <= fields)
    check("the route is the one we post to", '"/api/ahs-jobs/authorizations"' in src)


if __name__ == "__main__":
    test_the_template_is_an_authorization()
    test_spacing_dollar_signs_and_commas_do_not_matter()
    test_what_is_not_an_authorization()
    test_items_updated_is_a_possible_authorization()
    test_the_crm_body_and_its_dedupe_key()
    test_its_own_switch_off_by_default()
    test_the_poller()
    test_the_handler_skips_when_the_switch_is_off()
    test_the_adapter_posts_the_authorization()
    test_the_body_matches_the_crm_source()
    print("\nALL AHS AUTHORIZATION EMAIL CHECKS PASSED")
