"""Unit tests for the Quo-style Inbox decision core (per-contact threads).

PURE — no DB, no HTTP. Exercises app.services.inbox_threads (stdlib + app.services.sms):
  - per-CONTACT merging of message + call rows into one summary (newest-first);
  - derived UNREAD (inbound activity newer than last_read_at; NULL => everything unread);
  - derived OPEN with AUTO-REOPEN (activity newer than closed_at reopens);
  - derived RESPONDED (newest message not an unanswered inbound);
  - sticky-DID capture (the DID of the newest interaction) and from-number resolution:
    SMS = sticky if it passes the 10DLC gate else global-default fallback else blocked;
    calls = sticky else default.

NOT exercised here (require a live DB / HTTP — SAY SO): the /api/inbox endpoints wiring
these decisions to Postgres (state upserts, notes CRUD, send resolution), which reuse the
already-proven enqueue_outbound_message + queue paths.

Run: python -m tests.test_inbox_threads
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from app.services.inbox_threads import (
    DidRef,
    merge_threads,
    resolve_call_from,
    resolve_sms_from,
)

T0 = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"inbox_threads failed at: {name}")


def msg(cid, minutes, direction="inbound", body="hi", number_id="n1", phone="+1555",
        sms_enabled=True, campaign="C-1", caller_number="+1999"):
    return NS(caller_id=cid, direction=direction, body=body, received_at=at(minutes),
              number_id=number_id, number_phone=phone, sms_enabled=sms_enabled,
              sms_campaign_id=campaign, caller_number=caller_number)


def call(cid, minutes, direction="inbound", status="completed", number_id="n2", phone="+1666",
         caller_number="+1999"):
    return NS(caller_id=cid, direction=direction, status=status, started_at=at(minutes),
              duration_seconds=30, number_id=number_id, number_phone=phone,
              sms_enabled=False, sms_campaign_id=None, caller_number=caller_number)


def test_merge():
    print("per-contact merge:")
    threads = merge_threads(
        [msg("a", 10), msg("a", 5, direction="outbound"), msg("b", 8)],
        [call("a", 20), call("b", 1)],
    )
    check("one thread per contact", len(threads) == 2)
    check("newest-first order", threads[0].caller_id == "a")
    a = threads[0]
    check("counts split by kind", a.message_count == 2 and a.call_count == 1)
    check("newest activity wins the preview (call at +20)", a.last_kind == "call")
    check("sticky DID = DID of newest interaction", a.sticky.number_id == "n2")
    check("rows without caller_id are skipped",
          len(merge_threads([msg(None, 1)], [])) == 0)


def test_unread_and_read():
    print("derived unread:")
    rows = [msg("a", 10), msg("a", 20), msg("a", 30, direction="outbound")]
    t = merge_threads(rows, [call("a", 25)])[0]
    check("no state row => all inbound unread (2 msgs + 1 call)", t.unread_count == 3)
    t = merge_threads(rows, [call("a", 25)], {"a": (at(22), None)})[0]
    check("read at +22 => only the +25 call unread", t.unread_count == 1)
    t = merge_threads(rows, [call("a", 25)], {"a": (at(40), None)})[0]
    check("read after everything => 0 unread", t.unread_count == 0)
    check("outbound rows never count as unread",
          merge_threads([msg("a", 5, direction="outbound")], [])[0].unread_count == 0)


def test_open_close_reopen():
    print("derived open / auto-reopen:")
    rows = [msg("a", 10)]
    check("never closed => open", merge_threads(rows, [], {})[0].open)
    check("closed after last activity => closed",
          not merge_threads(rows, [], {"a": (None, at(15))})[0].open)
    check("activity NEWER than close => auto-reopened",
          merge_threads([msg("a", 10), msg("a", 20)], [], {"a": (None, at(15))})[0].open)


def test_blocked_and_deleted():
    print("derived blocked / soft-deleted:")
    rows = [msg("a", 10)]
    check("no state => not blocked, not deleted",
          not merge_threads(rows, [], {})[0].blocked
          and not merge_threads(rows, [], {})[0].deleted)
    check("blocked_at set => blocked (stays regardless of newer activity)",
          merge_threads([msg("a", 10), msg("a", 20)], [], {"a": (None, None, at(5), None)})[0].blocked)
    check("deleted_at after last activity => deleted",
          merge_threads(rows, [], {"a": (None, None, None, at(15))})[0].deleted)
    check("activity NEWER than delete => auto-reappears (not deleted)",
          not merge_threads([msg("a", 10), msg("a", 20)], [], {"a": (None, None, None, at(15))})[0].deleted)
    check("2-tuple state still works (back-compat)",
          not merge_threads(rows, [], {"a": (at(5), None)})[0].blocked)


def test_responded():
    print("derived responded:")
    check("last message inbound => unresponded",
          not merge_threads([msg("a", 10)], [])[0].responded)
    check("last message outbound => responded",
          merge_threads([msg("a", 10), msg("a", 11, direction="outbound")], [])[0].responded)
    check("calls don't affect responded (msg-only signal)",
          not merge_threads([msg("a", 10)], [call("a", 99)])[0].responded)


STICKY_OK = DidRef("n1", "+1555", True, "C-1")
STICKY_NO_SMS = DidRef("n2", "+1666", False, None)
DEFAULT_OK = DidRef("n3", "+1777", True, "C-2")
DEFAULT_NO_SMS = DidRef("n4", "+1888", False, None)


def test_sms_resolution():
    print("SMS from-number resolution (10DLC gate + fallback):")
    d, fb, reason = resolve_sms_from(STICKY_OK, DEFAULT_OK)
    check("sticky passes gate => sticky, no fallback", d.number_id == "n1" and not fb)
    d, fb, reason = resolve_sms_from(STICKY_NO_SMS, DEFAULT_OK)
    check("sticky blocked => default with fallback flag", d.number_id == "n3" and fb)
    d, fb, reason = resolve_sms_from(STICKY_NO_SMS, DEFAULT_NO_SMS)
    check("both blocked => None + reason", d is None and bool(reason))
    d, fb, reason = resolve_sms_from(None, DEFAULT_OK)
    check("no sticky (new contact) => default", d.number_id == "n3" and fb)
    d, fb, reason = resolve_sms_from(None, None)
    check("nothing available => blocked with reason", d is None and bool(reason))


def test_call_resolution():
    print("call from-number resolution:")
    check("sticky wins even without SMS capability",
          resolve_call_from(STICKY_NO_SMS, DEFAULT_OK).number_id == "n2")
    check("no sticky => default", resolve_call_from(None, DEFAULT_OK).number_id == "n3")
    check("nothing => None", resolve_call_from(None, None) is None)


if __name__ == "__main__":
    test_merge()
    test_unread_and_read()
    test_open_close_reopen()
    test_blocked_and_deleted()
    test_responded()
    test_sms_resolution()
    test_call_resolution()
    print("inbox_threads: all checks passed")
