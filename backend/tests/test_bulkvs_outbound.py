"""Unit tests for the manual-outbound-SMS decision core (Ticket 10).

PURE — no DB, no HTTP. Exercises the logic every acceptance criterion turns on, which lives in
app.services.sms (stdlib-only, importable in a bare sandbox):
  - the per-number 10DLC send GATE (outbound_block_reason): enabled+campaign -> allowed;
    not-enabled or no-campaign -> refused with a reason;
  - STOP/START/HELP keyword classification + opt-out STATE transitions (classify_keyword,
    next_optout_state, is_opted_out) — the exact logic apply_inbound_keyword folds into the
    sms_opt_outs row, and that the send gate blocks an opted-out contact;
  - forward-only outbound delivery STATUS advance (advance_status) — the exact logic the
    /webhooks/bulkvs/message-status handler applies.
If the provider client imports cleanly (httpx present) the /messageSend RefId extractor is
also checked; it is SKIPPED (not failed) in a bare sandbox.

NOT exercised here (require a live DB / HTTP, sqlalchemy is absent in this sandbox — SAY SO):
  - enqueue_outbound_message writing the outbound `messages` row (direction='outbound',
    sent_by_user_id) + api.send_message enqueuing the `message_send` job;
  - apply_inbound_keyword upserting sms_opt_outs on the inbound path;
  - handle_message_send calling BulkVS + enqueuing message_relay_ghl (the GHL relay);
  - the message-status webhook row lookup.
Those wire the PROVEN pure decisions above to the DB/queue; the decisions themselves are here.

Run: python -m tests.test_bulkvs_outbound
"""

import sys

from app.services import sms


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"bulkvs_outbound failed at: {name}")


def test_gate():
    print("per-number 10DLC send gate (outbound_block_reason):")
    check("enabled + campaign -> allowed (None)",
          sms.outbound_block_reason(True, "C-123") is None)
    check("not enabled -> refused", sms.outbound_block_reason(False, "C-123") is not None)
    check("enabled but no campaign -> refused",
          sms.outbound_block_reason(True, None) is not None)
    check("enabled but blank campaign -> refused",
          sms.outbound_block_reason(True, "") is not None)


def test_keyword_classification():
    print("STOP/START/HELP classification:")
    check("STOP -> stop", sms.classify_keyword("STOP") == "stop")
    check("lower-case stop -> stop", sms.classify_keyword("stop") == "stop")
    check("padded ' Stop ' -> stop", sms.classify_keyword("  Stop  ") == "stop")
    check("UNSUBSCRIBE -> stop", sms.classify_keyword("unsubscribe") == "stop")
    check("START -> start", sms.classify_keyword("START") == "start")
    check("YES -> start", sms.classify_keyword("yes") == "start")
    check("HELP -> help", sms.classify_keyword("HELP") == "help")
    check("plain text -> None", sms.classify_keyword("can you send a quote?") is None)
    check("keyword embedded in a sentence is NOT a command",
          sms.classify_keyword("please stop by tomorrow") is None)
    check("empty -> None", sms.classify_keyword("") is None)
    check("None -> None", sms.classify_keyword(None) is None)


def test_optout_transitions():
    print("opt-out state transitions + block:")
    # STOP opts a contact out; the send gate then blocks it.
    after_stop = sms.next_optout_state(None, sms.classify_keyword("STOP"))
    check("STOP -> opted_out", after_stop == sms.OPTED_OUT)
    check("opted_out is blocked", sms.is_opted_out(after_stop) is True)

    # START opts back in; the gate no longer blocks.
    after_start = sms.next_optout_state(after_stop, sms.classify_keyword("START"))
    check("START -> opted_in", after_start == sms.OPTED_IN)
    check("opted_in is NOT blocked", sms.is_opted_out(after_start) is False)

    # HELP does not change state.
    check("HELP leaves opted_out unchanged",
          sms.next_optout_state(sms.OPTED_OUT, sms.classify_keyword("HELP")) == sms.OPTED_OUT)
    check("HELP leaves opted_in unchanged",
          sms.next_optout_state(sms.OPTED_IN, sms.classify_keyword("help")) == sms.OPTED_IN)

    # No row (never interacted) is not blocked.
    check("no opt-out row -> not blocked", sms.is_opted_out(None) is False)


def test_status_forward_only():
    print("forward-only outbound status advance:")
    check("queued -> sent advances", sms.advance_status("queued", "sent") == "sent")
    check("sent -> delivered advances", sms.advance_status("sent", "delivered") == "delivered")
    check("delivered -> sent does NOT regress",
          sms.advance_status("delivered", "sent") == "delivered")
    check("sent -> sent (equal rank) no change", sms.advance_status("sent", "sent") == "sent")
    check("sent -> queued (lower rank) ignored", sms.advance_status("sent", "queued") == "sent")
    check("unknown status ignored (keeps current)",
          sms.advance_status("sent", "banana") == "sent")
    check("sent -> failed (terminal peer) advances",
          sms.advance_status("sent", "failed") == "failed")
    check("case-insensitive new status", sms.advance_status("queued", "DELIVERED") == "delivered")


def test_ref_id_extraction():
    print("BulkVS /messageSend RefId extraction (skipped if httpx absent):")
    try:
        from app.providers.bulkvs_client import _extract_ref_id
    except Exception as exc:  # noqa: BLE001 - bare sandbox without httpx/pydantic: skip, don't fail
        print(f"  [SKIP] bulkvs_client not importable here ({exc.__class__.__name__})")
        return
    check("RefId key", _extract_ref_id({"RefId": "abc123"}) == "abc123")
    check("MessageRef alias", _extract_ref_id({"MessageRef": "z9"}) == "z9")
    check("missing ref -> None", _extract_ref_id({"nope": 1}) is None)
    check("non-dict -> None", _extract_ref_id(["x"]) is None)


def main():
    test_gate()
    test_keyword_classification()
    test_optout_transitions()
    test_status_forward_only()
    test_ref_id_extraction()
    print("\nALL BULKVS-OUTBOUND CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)
