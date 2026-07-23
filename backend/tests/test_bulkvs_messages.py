"""Unit tests for BulkVS inbound SMS/MMS + the Messages inbox thread grouping (Ticket 09).

Pure — no DB, no HTTP. Exercises:
  - BulkvsAdapter.parse_message_event: synthetic-SID synthesis (deterministic across retries),
    From/To E.164 normalization, To-as-array, MMS media collection, _tracking_number override;
  - source-IP verification: client_ip (X-Forwarded-For leftmost, peer fallback) + ip_allowed
    accepting an allow-listed BulkVS IP and rejecting others;
  - group_threads: collapses newest-first message rows into per-(number_id, caller_id) threads
    in newest-first order, with correct counts + latest-message preview.

Run: python -m tests.test_bulkvs_messages
"""

import sys
from datetime import datetime, timezone
from types import SimpleNamespace

from app.providers.bulkvs import (
    BULKVS_INBOUND_IPS,
    BulkvsAdapter,
    client_ip,
    ip_allowed,
)
from app.services.message_threads import group_threads


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"bulkvs_messages failed at: {name}")


def test_parse():
    print("BulkvsAdapter.parse_message_event:")
    a = BulkvsAdapter()

    p = {"From": "3055543333", "To": "3055540003", "Message": "quote on the truck?"}
    evt = a.parse_message_event(p)
    check("From normalized to E.164", evt.from_number == "+13055543333")
    check("To normalized to E.164", evt.to_number == "+13055540003")
    check("body carried", evt.body == "quote on the truck?")
    check("direction inbound", evt.direction == "inbound")
    check("synthetic sid is bulkvs-prefixed", evt.provider_message_sid.startswith("bulkvs-"))

    # Deterministic across retries: identical payload -> identical SID (idempotency key).
    check("synthetic sid deterministic",
          a.parse_message_event(dict(p)).provider_message_sid == evt.provider_message_sid)
    # Different body -> different SID.
    check("different body -> different sid",
          a.parse_message_event(dict(p, Message="other")).provider_message_sid
          != evt.provider_message_sid)

    # To may arrive as a single-element array.
    check("To-as-array normalized",
          a.parse_message_event({"From": "3055543333", "To": ["13055540003"],
                                 "Message": "hi"}).to_number == "+13055540003")

    # MMS media collected as-is; num_media derived.
    mms = a.parse_message_event({"From": "3055543333", "To": "3055540003", "Message": "pic",
                                 "Attachments": ["http://m/0.jpg", "http://m/1.jpg"]})
    check("MMS media collected", mms.media_urls == ["http://m/0.jpg", "http://m/1.jpg"])
    check("num_media derived from media", mms.num_media == 2)

    # _tracking_number query override wins over payload To (per-DID routing).
    over = a.parse_message_event({"From": "3055543333", "To": "9999999999",
                                  "Message": "x", "_tracking_number": "3055540003"})
    check("_tracking_number overrides payload To", over.to_number == "+13055540003")


def test_ip_verification():
    print("source-IP verification:")
    good = BULKVS_INBOUND_IPS[0]
    check("allow-listed IP accepted", ip_allowed(good, BULKVS_INBOUND_IPS) is True)
    check("second allow-listed IP accepted",
          ip_allowed(BULKVS_INBOUND_IPS[1], BULKVS_INBOUND_IPS) is True)
    check("random IP rejected", ip_allowed("8.8.8.8", BULKVS_INBOUND_IPS) is False)
    check("empty IP rejected", ip_allowed("", BULKVS_INBOUND_IPS) is False)

    # Behind Traefik: real client is the leftmost X-Forwarded-For entry.
    check("XFF leftmost is the client",
          client_ip(f"{good}, 10.0.0.1, 172.18.0.2", "172.18.0.2") == good)
    check("no XFF -> falls back to peer", client_ip(None, good) == good)
    check("verified end-to-end (XFF good IP)",
          ip_allowed(client_ip(good, "172.18.0.2"), BULKVS_INBOUND_IPS) is True)
    check("verified end-to-end (spoofed peer, bad XFF) rejected",
          ip_allowed(client_ip("1.2.3.4", good), BULKVS_INBOUND_IPS) is False)


def _row(number_id, caller_id, caller_number, body, received_at):
    return SimpleNamespace(
        number_id=number_id, caller_id=caller_id, caller_number=caller_number,
        number_phone="+13055540003", number_label="Roofing CL", campaign_name="MSG",
        provider="bulkvs", body=body, direction="inbound", received_at=received_at,
    )


def test_thread_grouping():
    print("group_threads:")
    t0 = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 7, 22, 11, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    # Newest-first (as the API fetches).
    rows = [
        _row("N1", "C1", "+13055543333", "third from C1", t2),
        _row("N1", "C2", "+13055549999", "only from C2", t1),
        _row("N1", "C1", "+13055543333", "first from C1", t0),
    ]
    threads = group_threads(rows)
    check("two distinct threads", len(threads) == 2)
    check("newest thread first (C1)", threads[0].caller_id == "C1")
    check("thread preview is latest message", threads[0].last_body == "third from C1")
    check("thread last_at is latest", threads[0].last_at == t2)
    check("count aggregates thread messages", threads[0].message_count == 2)
    check("second thread is C2 single", threads[1].caller_id == "C2" and threads[1].message_count == 1)
    check("attribution fields carried", threads[0].number_label == "Roofing CL")

    # NULL number_id/caller_id is a valid, distinct thread key.
    threads2 = group_threads([_row(None, None, None, "orphan", t0)])
    check("NULL key thread grouped", len(threads2) == 1 and threads2[0].number_id is None)


def main():
    test_parse()
    test_ip_verification()
    test_thread_grouping()
    print("\nALL BULKVS-MESSAGE CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)
