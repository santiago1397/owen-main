"""Unit test for Asterisk ARI ingestion (ticket 04) — mapping + entry-channel ranking +
dedup, feeding the SAME event-sourced projection as Twilio/SignalWire.

Dependency-free by design: exercises the pure `AsteriskEventRouter` (app/providers/asterisk.py)
and a tiny in-memory stand-in for `ingest_status_event`'s forward-only, one-row-per-SID
projection. It does NOT hit Postgres — the real `ingest_status_event` needs sqlalchemy +
asyncpg which aren't in this sandbox — so we assert the routing/normalization contract that
feeds it, and that every status the adapter emits is already in the shared STATUS_RANK
vocabulary (i.e. Twilio/SignalWire projection semantics are reused, not forked).

Run: python -m tests.test_asterisk_ingestion
"""

import sys

from app.providers.asterisk import (
    _ARI_TO_STATUS,
    _CAUSE_TO_STATUS,
    _STATE_TO_STATUS,
    AsteriskAdapter,
    AsteriskEventRouter,
)
from app.providers.base import STATUS_RANK


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"asterisk_ingestion failed at: {name}")


LINKEDID = "1690000000.1"          # entry channel's Uniqueid == the call's Linkedid
ENTRY = "1690000000.1"
LEG2 = "1690000000.2"              # a secondary (e.g. dialed operator) leg, same Linkedid


def _evt(etype, chan_id, *, state=None, cause=None, ts=None):
    """Build a raw ARI channel event dict as it arrives over the WS. Every channel carries
    channelvars.Linkedid (ari.conf `channelvars=Linkedid`), which is how legs collapse."""
    return {
        "type": etype,
        "timestamp": ts or "2026-07-22T14:03:11.123+0000",
        "cause": cause,
        "channel": {
            "id": chan_id,
            "state": state,
            "caller": {"number": "+13055551234"},
            "dialplan": {"exten": "+13055559999"},  # the DID dialed = tracking number
            "channelvars": {"Linkedid": LINKEDID},
        },
    }


class _FakeProjection:
    """Mirrors ingest_status_event's invariants: one row per provider_call_sid, status
    advances only when the new rank strictly outranks the stored one (never regresses)."""

    def __init__(self):
        self.rows = {}  # sid -> {"status", "rank", "events": int}

    def ingest(self, evt):
        row = self.rows.get(evt.provider_call_sid)
        if row is None:
            self.rows[evt.provider_call_sid] = {
                "status": evt.status, "rank": evt.status_rank, "events": 1,
            }
            return
        row["events"] += 1
        if evt.status_rank > row["rank"]:
            row["status"], row["rank"] = evt.status, evt.status_rank


def test_vocabulary_is_shared():
    print("shared vocabulary (asterisk projects into the Twilio-CallStatus ranks):")
    produced = set()
    produced.update(v for v in _ARI_TO_STATUS.values() if v and v != "__cause__")
    produced.update(_STATE_TO_STATUS.values())
    produced.update(_CAUSE_TO_STATUS.values())
    produced.add("failed")  # ChannelDestroyed default
    unknown = {s for s in produced if s not in STATUS_RANK}
    check("every asterisk status is a known STATUS_RANK key", not unknown)


def test_noop_signature():
    print("no-op signature verification:")
    a = AsteriskAdapter()
    check("verify_signature is a no-op returning True",
          a.verify_signature("ws://x", {}, "") is True)
    check("adapter name is 'asterisk'", a.name == "asterisk")


def test_full_call_collapses_and_projects():
    print("full call — legs collapse under one Linkedid, correct status projection:")
    router = AsteriskEventRouter()
    proj = _FakeProjection()

    stream = [
        _evt("StasisStart", ENTRY),                       # -> initiated (entry)
        _evt("ChannelStateChange", ENTRY, state="Ringing"),  # -> ringing (entry)
        _evt("StasisStart", LEG2),                        # secondary leg -> SKIP
        _evt("ChannelStateChange", ENTRY, state="Up"),    # -> in-progress (entry)
        _evt("ChannelStateChange", LEG2, state="Up"),     # secondary leg -> SKIP
        _evt("ChannelStateChange", ENTRY, state="Up"),    # duplicate -> dedup SKIP
        _evt("ChannelDestroyed", ENTRY, cause=16),        # -> completed (entry)
        _evt("ChannelDestroyed", LEG2, cause=16),         # secondary leg -> SKIP
    ]

    routed = []
    for e in stream:
        evt = router.route(e)
        if evt is not None:
            routed.append(evt)
            proj.ingest(evt)

    statuses = [e.status for e in routed]
    check("only entry-channel, non-duplicate events routed",
          statuses == ["initiated", "ringing", "in-progress", "completed"])
    check("exactly one calls row per Linkedid (all legs collapse)", len(proj.rows) == 1)
    row = proj.rows[LINKEDID]
    check("provider_call_sid == Linkedid", LINKEDID in proj.rows)
    check("final projected status is terminal 'completed' (rank 4)",
          row["status"] == "completed" and row["rank"] == 4)
    check("all routed events point at the same provider_call_sid",
          {e.provider_call_sid for e in routed} == {LINKEDID})
    check("attribution carried: to=DID, from=caller",
          routed[0].to_number == "+13055559999" and routed[0].from_number == "+13055551234")
    check("direction is inbound (asterisk entry leg)",
          all(e.direction == "inbound" for e in routed))


def test_dedup_suppresses_repeats():
    print("dedup on '{Linkedid}:{status}' suppresses repeats:")
    router = AsteriskEventRouter()
    e = _evt("ChannelStateChange", ENTRY, state="Up")
    first = router.route(e)
    second = router.route(e)          # identical repeat
    third = router.route(_evt("ChannelStateChange", ENTRY, state="Up"))  # re-sent
    check("first Up routes", first is not None and first.status == "in-progress")
    check("dedup key is '{Linkedid}:{status}'", first.provider_sequence == f"{LINKEDID}:in-progress")
    check("repeat suppressed", second is None)
    check("re-sent duplicate suppressed", third is None)


def test_terminal_cause_mapping():
    print("terminal ChannelDestroyed cause -> status:")
    cases = {16: "completed", 17: "busy", 18: "no-answer", 19: "no-answer",
             21: "no-answer", 99: "failed", None: "failed"}
    for cause, expected in cases.items():
        router = AsteriskEventRouter()  # fresh so dedup doesn't cross cases
        evt = router.route(_evt("ChannelDestroyed", ENTRY, cause=cause))
        check(f"cause {cause} -> {expected}", evt is not None and evt.status == expected)


def test_non_entry_and_unmapped_skipped():
    print("non-entry legs and unmapped events are skipped:")
    router = AsteriskEventRouter()
    check("secondary-leg StasisStart skipped (not entry)",
          router.route(_evt("StasisStart", LEG2)) is None)
    check("StasisEnd is not terminal on its own -> skipped",
          router.route(_evt("StasisEnd", ENTRY)) is None)
    check("unknown event type skipped",
          router.route(_evt("ChannelDtmfReceived", ENTRY)) is None)
    check("ChannelStateChange with irrelevant state skipped",
          router.route(_evt("ChannelStateChange", ENTRY, state="Down")) is None)


def main():
    test_vocabulary_is_shared()
    test_noop_signature()
    test_full_call_collapses_and_projects()
    test_dedup_suppresses_repeats()
    test_terminal_cause_mapping()
    test_non_entry_and_unmapped_skipped()
    print("\nALL ASTERISK INGESTION CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)
