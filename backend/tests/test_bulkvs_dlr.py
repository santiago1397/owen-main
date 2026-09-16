"""Carrier delivery receipts are receipts, not messages (2026-09-16).

THE BUG, measured on production the morning after texting went live: BulkVS posts delivery
receipts to the MO webhook, owen-main stored each one as an INBOUND message and relayed it
to the CRM, and a customer's own thread ended up holding two texts they never wrote:

    id:1162999967 sub:001 dlvrd:000 submit date:2609160247 done date:2609160247
    stat:UNDELIV err:255 text:Dream Te...

What is proved here, in the order it matters:

  1. **A receipt is recognised, and a customer's text never is.** The match is the guard —
     everything downstream deletes, hides or reclassifies on the strength of it.
  2. **It is never stored or relayed as a message**, and the webhook still answers 200.
  3. **It correlates to the right outbound message** — NOT on the id, which is a different
     identifier space from the send's RefId (measured: RefId 4551F89F, DLR id 1162999967).
  4. **It advances the status forward-only** and reaches the CRM with a sentence a roofer
     can read.
  5. **It is idempotent**: a carrier re-POSTing the same receipt changes nothing.
  6. **A receipt that correlates to nothing is kept, hidden** — never dropped, never shown.
  7. **Nothing existing moved.**

Stdlib plus the app, like every other test here. No network, no real database: the DB is a
small in-memory double that answers the two queries `services/dlr` actually makes.

Run: python -m tests.test_bulkvs_dlr
"""

import asyncio
import inspect
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

OUR_DID = "+19544829099"
CUSTOMER = "+19415550101"

# The exact body measured on production, and its delivered sibling.
UNDELIV = ("id:1162999967 sub:001 dlvrd:000 submit date:2609160247 done date:2609160247 "
           "stat:UNDELIV err:255 text:Dream Te...")
DELIVRD = ("id:1162999968 sub:001 dlvrd:001 submit date:2609160247 done date:2609160248 "
           "stat:DELIVRD err:000 text:Dream Te...")


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"bulkvs_dlr failed at: {name}")


def payload(message: str, frm: str = CUSTOMER, to: str = OUR_DID, **extra) -> dict:
    body = {"From": frm.lstrip("+"), "To": [to.lstrip("+")], "Message": message}
    body.update(extra)
    return body


# --- a message double ---------------------------------------------------------------------


@dataclass
class FakeMessage:
    id: str
    direction: str
    from_number: str
    to_number: str
    body: str
    status: str
    received_at: datetime
    raw_payload: dict | None = None


@dataclass
class FakeDb:
    """Answers the one SELECT `services/dlr.correlate` makes, and records commits."""

    rows: list = field(default_factory=list)
    commits: int = 0

    async def execute(self, stmt):
        db = self

        class Result:
            def scalars(self_inner):
                class S:
                    def all(s2):
                        # `correlate` filters in Python beyond the window; the double
                        # returns every outbound row and lets the real code do its job.
                        return [r for r in db.rows if r.direction == "outbound"]
                return S()
        return Result()

    async def commit(self):
        self.commits += 1


def an_outbound(body="Dream Team Roofing: we can be there Tuesday", minutes_ago=3,
                status="sent", raw=None, to=CUSTOMER):
    return FakeMessage(
        id="m-%d" % (abs(hash((body, minutes_ago, to))) % 10000),
        direction="outbound", from_number=OUR_DID, to_number=to, body=body, status=status,
        received_at=datetime(2026, 9, 16, 2, 47, tzinfo=timezone.utc)
        - timedelta(minutes=minutes_ago),
        raw_payload=raw)


# --- 1. what is a receipt ------------------------------------------------------------------


def test_a_receipt_is_recognised_and_a_customers_text_never_is():
    from app.providers.bulkvs import parse_delivery_receipt

    print("telling a receipt from a message:")
    got = parse_delivery_receipt(payload(UNDELIV))
    check("the measured production body parses", got is not None)
    check("the receipt id is read", got.receipt_id == "1162999967")
    check("the carrier status is read", got.stat == "UNDELIV")
    check("the error code is read", got.err == "255")
    check("the submit date is read", got.submit_date == "2609160247")
    check("the text echo is read", got.text_prefix == "Dream Te...")
    check("From is the RECIPIENT, not us", got.customer_number == CUSTOMER)
    check("To is our own DID", got.dialed_number == OUR_DID)
    check("a delivered receipt is not 'failed'",
          parse_delivery_receipt(payload(DELIVRD)).failed is False)
    check("an undelivered one is", got.failed is True)

    # THE GUARD. Everything downstream hides or deletes on the strength of this.
    for text in (
        "can you come Tuesday?",
        "id:",
        "My id:12 is on the invoice",
        "stat:DELIVRD",
        "id:1 sub:1 dlvrd:1 stat:DELIVRD",                      # missing fields
        "please call me about id:99 sub:001 dlvrd:000",          # not anchored
        "",
        "Dream Team Roofing: your appointment is submit date:tomorrow",
    ):
        check("a customer could write %r and it is NOT a receipt" % text[:34],
              parse_delivery_receipt(payload(text)) is None)


def test_a_carrier_variant_we_cannot_read_is_made_loud():
    from app.providers.bulkvs import looks_like_unparsed_receipt

    print("an unrecognised variant:")
    check("a receipt-shaped body that does not parse is flagged",
          looks_like_unparsed_receipt(payload("id:99 sub:001 stat:DELIVRD")))
    check("a real receipt is not flagged (it parsed)",
          not looks_like_unparsed_receipt(payload(UNDELIV)))
    check("an ordinary text is not flagged",
          not looks_like_unparsed_receipt(payload("can you come Tuesday?")))


def test_a_payload_that_declares_itself_is_read_but_never_overrides_the_body():
    from app.providers.bulkvs import flagged_as_receipt, parse_delivery_receipt

    print("the payload flag (a second route, never the first):")
    check("a declared type is reported by name",
          flagged_as_receipt(payload(UNDELIV, MessageType="DLR")) == "MessageType")
    check("an ordinary payload declares nothing", flagged_as_receipt(payload(UNDELIV)) == "")
    # The flag must not be able to turn a customer's text into a receipt: the names are
    # plausible rather than observed, and a wrong guess must cost nothing.
    check("a flag alone does NOT reclassify a customer's message",
          parse_delivery_receipt(payload("can you come Tuesday?", MessageType="DLR")) is None)


# --- 2. it never becomes a message ---------------------------------------------------------


def test_the_webhook_takes_the_receipt_branch_before_anything_is_stored():
    from app.webhooks import bulkvs as hook

    print("POST /webhooks/bulkvs/message:")
    src = inspect.getsource(hook.message)
    check("the receipt is parsed before the ingest upsert",
          src.index("parse_delivery_receipt") < src.index("ingest_message_event"))
    check("and before the CRM is asked anything",
          src.index("parse_delivery_receipt") < src.index("crm_hook.handle_inbound_message"))
    check("a receipt returns immediately — no ingest, no GHL relay, no CRM hook",
          "await _take_receipt(receipt)\n        return Response(status_code=200)" in src)
    check("it still answers 200", "return Response(status_code=200)" in src)
    check("an unreadable variant is logged at ERROR",
          "logger.error" in src and "looks_like_unparsed_receipt" in src)
    total = inspect.getsource(hook._take_receipt)
    check("applying a receipt can never cost the webhook its 200",
          "except Exception" in total)


def test_an_orphan_receipt_is_kept_hidden_and_never_relayed():
    from app.webhooks import bulkvs as hook

    print("a receipt that correlates to nothing:")
    src = inspect.getsource(hook._keep_orphan_receipt)
    check("it is stored rather than dropped", "ingest_message_event" in src)
    check("marked as junk so no thread shows it", "DLR_JUNK_KEY" in src)
    check("and never relayed onward", "relayed_to_ghl = True" in src)
    check("the reason it could not be placed is kept with it", "uncorrelated" in src)


def test_the_inbox_never_shows_a_stored_receipt():
    from app.api import inbox

    print("the Inbox query:")
    src = inspect.getsource(inbox._msg_stmt)
    check("junk rows are excluded", "DLR_JUNK_KEY" in src)
    check("in the ONE statement both the list and the thread view use",
          "_msg_stmt()" in inspect.getsource(inbox.list_threads)
          and "_msg_stmt()" in inspect.getsource(inbox.get_thread))
    # NULL ? 'key' is NULL, and NOT NULL is NULL — a bare ~has_key would hide every
    # outbound row the CRM did not send, which is most of the operator's Inbox.
    compiled = str(inbox._msg_stmt())
    check("and the filter is NULL-safe", "raw_payload IS NULL OR NOT" in compiled)


# --- 3 + 4. correlation, and what the operator reads ---------------------------------------


def test_it_correlates_on_the_did_recipient_text_and_time_never_on_the_id():
    from app.providers.bulkvs import parse_delivery_receipt
    from app.services import dlr

    print("correlation:")
    receipt = parse_delivery_receipt(payload(UNDELIV))
    right = an_outbound(minutes_ago=3)
    db = FakeDb(rows=[
        an_outbound(body="A different text entirely", minutes_ago=4),
        an_outbound(to="+19415559999", minutes_ago=2),          # another customer
        right,
    ])
    found = asyncio.run(dlr.correlate(db, receipt))
    check("the right message is found", found.matched and found.message is right)
    check("and it says how", "text echo" in found.how and "submit time" in found.how)

    # The thing this must NOT do. The ids are different spaces: RefId 4551F89F (hex, 8) vs
    # DLR id 1162999967 (decimal, 10).
    src = inspect.getsource(dlr.correlate)
    check("nothing joins on the receipt id",
          "provider_message_sid" not in src and "receipt_id" not in src.split("seen_marker")[0])

    check("a receipt for a number we never texted matches nothing",
          not asyncio.run(dlr.correlate(FakeDb(rows=[an_outbound(to="+19415559999")]),
                                        receipt)).matched)
    check("a receipt whose text echo matches nothing we sent matches nothing",
          not asyncio.run(dlr.correlate(FakeDb(rows=[an_outbound(body="Something else")]),
                                        receipt)).matched)
    check("a receipt naming no DID matches nothing",
          not asyncio.run(dlr.correlate(
              FakeDb(rows=[right]), parse_delivery_receipt(payload(UNDELIV, To=[])))).matched)


def test_the_nearest_send_in_time_wins_when_the_same_text_went_twice():
    from app.providers.bulkvs import parse_delivery_receipt
    from app.services import dlr

    print("ambiguity is resolved, not guessed:")
    near = an_outbound(minutes_ago=1)
    far = an_outbound(minutes_ago=600)
    found = asyncio.run(dlr.correlate(FakeDb(rows=[far, near]),
                                      parse_delivery_receipt(payload(UNDELIV))))
    check("the send nearest the receipt's submit time is taken", found.message is near)
    check("and the report says the choice was made", "2 messages matched" in found.how)


def test_the_status_and_the_sentence_the_operator_reads():
    from app.providers.bulkvs import parse_delivery_receipt
    from app.services import dlr

    print("what the carrier's word becomes:")
    undeliv = parse_delivery_receipt(payload(UNDELIV))
    delivrd = parse_delivery_receipt(payload(DELIVRD))
    check("DELIVRD -> delivered", dlr.status_for(delivrd) == "delivered")
    check("UNDELIV -> undelivered", dlr.status_for(undeliv) == "undelivered")
    for stat, expected in (("REJECTD", "failed"), ("EXPIRED", "failed"),
                           ("DELETED", "failed"), ("UNKNOWN", "failed"),
                           ("ACCEPTD", "sent")):
        got = parse_delivery_receipt(payload(UNDELIV.replace("stat:UNDELIV", "stat:" + stat)))
        check("%s -> %s" % (stat, expected), dlr.status_for(got) == expected)
    # A carrier word nobody here has seen must not read as good news.
    weird = parse_delivery_receipt(payload(UNDELIV.replace("stat:UNDELIV", "stat:WOBBLE")))
    check("an unknown carrier word is pessimistic, not optimistic",
          dlr.status_for(weird) == "failed")

    check("a delivered receipt needs no sentence", dlr.detail_for(delivrd) == "")
    check("an undelivered one says so, with the carrier's error code",
          dlr.detail_for(undeliv) == "the carrier could not deliver it (error 255)")
    rejected = parse_delivery_receipt(
        payload(UNDELIV.replace("stat:UNDELIV", "stat:REJECTD")))
    check("a rejection reads as the operator was promised",
          dlr.detail_for(rejected) == "the carrier rejected it (error 255)")
    no_err = parse_delivery_receipt(payload(UNDELIV.replace("err:255", "err:000")))
    check("err:000 adds no error code — it means nothing went wrong",
          dlr.detail_for(no_err) == "the carrier could not deliver it")
    check("the code is carried verbatim and not interpreted",
          "255" in dlr.detail_for(undeliv) and "unknown error" not in dlr.detail_for(undeliv))


def test_applying_it_advances_the_row_forward_only_and_tells_the_crm():
    from app.integrations.crm import hook as crm_hook
    from app.providers.bulkvs import parse_delivery_receipt
    from app.services import dlr

    print("applying:")
    seen = []

    async def fake_relay(*, message_id, status, detail=""):
        seen.append({"message_id": message_id, "status": status, "detail": detail})
        return True

    real = crm_hook.handle_delivery_receipt
    crm_hook.handle_delivery_receipt = fake_relay
    try:
        row = an_outbound(status="sent")
        db = FakeDb(rows=[row])
        out = asyncio.run(dlr.apply(db, parse_delivery_receipt(payload(UNDELIV))))
        check("it was applied", out["applied"])
        check("the row advanced sent -> undelivered", row.status == "undelivered")
        check("and was committed", db.commits == 1)
        check("the CRM was told", len(seen) == 1 and seen[0]["status"] == "undelivered")
        check("with the sentence, not the carrier's jargon",
              seen[0]["detail"] == "the carrier could not deliver it (error 255)")
        check("keyed on messages.id — the CRM's provider_ref",
              seen[0]["message_id"] == str(row.id))

        # FORWARD-ONLY. A late receipt must not walk a delivered message backwards.
        delivered = an_outbound(status="delivered")
        asyncio.run(dlr.apply(FakeDb(rows=[delivered]),
                              parse_delivery_receipt(payload(UNDELIV))))
        check("a late failure cannot un-deliver a delivered message",
              delivered.status == "delivered")
    finally:
        crm_hook.handle_delivery_receipt = real


def test_the_same_receipt_twice_changes_nothing():
    """BulkVS re-POSTs a receipt it did not get a 200 for. The second one must be free."""
    from app.integrations.crm import hook as crm_hook
    from app.providers.bulkvs import parse_delivery_receipt
    from app.services import dlr

    print("idempotency:")
    calls = []

    async def fake_relay(**kw):
        calls.append(kw)
        return True

    real = crm_hook.handle_delivery_receipt
    crm_hook.handle_delivery_receipt = fake_relay
    try:
        row = an_outbound(status="sent")
        db = FakeDb(rows=[row])
        receipt = parse_delivery_receipt(payload(UNDELIV))
        asyncio.run(dlr.apply(db, receipt))
        commits, relays, status = db.commits, len(calls), row.status

        again = asyncio.run(dlr.apply(db, receipt))
        check("the second delivery is a no-op", not again["applied"])
        check("and says why", again["reason"] == "already applied")
        check("nothing was written again", db.commits == commits)
        check("the CRM was not told twice", len(calls) == relays)
        check("the status is unchanged", row.status == status)
    finally:
        crm_hook.handle_delivery_receipt = real


def test_the_whole_raw_payload_is_kept_beside_the_crm_marker():
    """Which field BulkVS flags a receipt with could not be read from here. This is where
    the next live one writes the answer down — and the CRM-link marker must survive it,
    because it is the only thing that says a message was the CRM's."""
    from app.integrations.crm import config as crm_config
    from app.providers.bulkvs import parse_delivery_receipt
    from app.services import dlr

    print("evidence:")
    marker = crm_config.link_marker("link-1", OUR_DID)
    row = an_outbound(raw=dict(marker))
    dlr.remember(row, parse_delivery_receipt(payload(UNDELIV, MessageType="DLR")))
    check("the CRM-link marker survives", crm_config.marker_of(row.raw_payload) is not None)
    check("the receipt is recorded", row.raw_payload["bulkvs_dlr"]["id"] == "1162999967")
    check("with the WHOLE payload, flag field and all",
          row.raw_payload["bulkvs_dlr"]["raw"].get("MessageType") == "DLR")
    check("and an idempotency marker",
          row.raw_payload["bulkvs_dlr_seen"] == ["1162999967:UNDELIV"])
    # SQLAlchemy does not track mutation inside a JSONB value; a new dict must be assigned.
    check("a NEW dict is assigned, not the old one mutated",
          "dict(message.raw_payload or {})" in inspect.getsource(dlr.remember))


def test_the_submit_time_is_treated_as_a_hint_and_never_as_the_truth():
    from app.providers.bulkvs import parse_delivery_receipt
    from app.services import dlr

    print("the submit date:")
    got = dlr.submit_instant(parse_delivery_receipt(payload(UNDELIV)))
    check("YYMMDDhhmm parses", got == datetime(2026, 9, 16, 2, 47, tzinfo=timezone.utc))
    twelve = UNDELIV.replace("submit date:2609160247", "submit date:260916024700")
    check("YYMMDDhhmmss parses too",
          dlr.submit_instant(parse_delivery_receipt(payload(twelve)))
          == datetime(2026, 9, 16, 2, 47, tzinfo=timezone.utc))
    bad = UNDELIV.replace("submit date:2609160247", "submit date:99999999")
    check("an unreadable date is None rather than a guess",
          dlr.submit_instant(parse_delivery_receipt(payload(bad))) is None)
    # The SMSC's timezone is not stated, so the window must be wide enough to absorb it.
    check("the correlation window is wider than any timezone offset",
          dlr._WINDOW >= timedelta(hours=26))
    check("and a receipt whose date cannot be read still correlates on the rest",
          asyncio.run(dlr.correlate(FakeDb(rows=[an_outbound()]),
                                    parse_delivery_receipt(payload(bad)))).matched)


# --- 5. the backfill ------------------------------------------------------------------------


def test_the_backfill_is_a_dry_run_that_never_deletes():
    from app.scripts import backfill_dlrs

    print("app.scripts.backfill_dlrs:")
    src = inspect.getsource(backfill_dlrs)
    check("dry run by default", '"--apply", action="store_true"' in src)
    check("it deletes NOTHING", "db.delete" not in src and ".delete(" not in src)
    check("it hides instead", "DLR_JUNK_KEY" in src)
    check("it uses the SAME parser as the webhook — one definition of a receipt",
          "from app.providers.bulkvs import parse_delivery_receipt" in src)
    check("and the SAME correlation as the webhook",
          "from app.services import dlr" in src and "dlr.apply" in src)
    check("it only ever looks at inbound rows", 'Message.direction == "inbound"' in src)
    check("a row that is not a receipt is skipped untouched",
          "if receipt is None:\n                continue" in src)
    check("already-hidden rows are not re-counted", "is_junk(message.raw_payload)" in src)
    check("the dry run predicts the real run's numbers", "_preview" in src)
    check("the report is counts only — no body, no number, no name",
          "%d" in src and "m.body" not in src and "from_number" not in src.split("def _as_payload")[1].split("async def run")[1])
    check("it can be run without the CRM", '"--no-crm"' in src)


# --- 6. nothing existing moved ---------------------------------------------------------------


def test_the_ordinary_inbound_path_is_untouched():
    from app.webhooks import bulkvs as hook

    print("a real customer text still takes the path it always did:")
    src = inspect.getsource(hook.message)
    check("it is still ingested", "await ingest_message_event(db, \"bulkvs\", evt)" in src)
    check("STOP/START still maintain the opt-out", "apply_inbound_keyword" in src)
    check("the GoHighLevel relay is still enqueued first",
          src.index("message_relay_ghl") < src.index("crm_hook.handle_inbound_message"))
    check("the CRM first-sight guard is still there", "if crm_first_sight:" in src)
    check("and the DLR branch is the only thing added before it",
          src.index("parse_delivery_receipt") < src.index("_adapter.parse_message_event"))


def test_the_message_status_webhook_still_works_the_way_it_did():
    """/webhooks/bulkvs/message-status is a different route for a different shape, and this
    change does not touch it — a RefId-keyed DLR, if BulkVS ever sends one, still lands."""
    from app.webhooks import bulkvs as hook

    print("the DLR webhook that already existed:")
    src = inspect.getsource(hook.message_status)
    check("it still matches on the RefId", 'f"bulkvs-{ref}"' in src)
    check("it still advances forward-only", "sms.advance_status" in src)
    check("it still relays to the CRM", "crm_hook.handle_delivery_receipt" in src)


def main():
    test_a_receipt_is_recognised_and_a_customers_text_never_is()
    test_a_carrier_variant_we_cannot_read_is_made_loud()
    test_a_payload_that_declares_itself_is_read_but_never_overrides_the_body()
    test_the_webhook_takes_the_receipt_branch_before_anything_is_stored()
    test_an_orphan_receipt_is_kept_hidden_and_never_relayed()
    test_the_inbox_never_shows_a_stored_receipt()
    test_it_correlates_on_the_did_recipient_text_and_time_never_on_the_id()
    test_the_nearest_send_in_time_wins_when_the_same_text_went_twice()
    test_the_status_and_the_sentence_the_operator_reads()
    test_applying_it_advances_the_row_forward_only_and_tells_the_crm()
    test_the_same_receipt_twice_changes_nothing()
    test_the_whole_raw_payload_is_kept_beside_the_crm_marker()
    test_the_submit_time_is_treated_as_a_hint_and_never_as_the_truth()
    test_the_backfill_is_a_dry_run_that_never_deletes()
    test_the_ordinary_inbound_path_is_untouched()
    test_the_message_status_webhook_still_works_the_way_it_did()
    print("\nALL BULKVS DELIVERY-RECEIPT CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        print(exc)
        sys.exit(1)
