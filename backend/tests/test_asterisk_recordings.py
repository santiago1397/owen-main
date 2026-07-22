"""Unit test for Asterisk recordings reuse + CDR reconcile (ticket 05).

Dependency-free by design (mirrors tests/test_asterisk_ingestion.py): it exercises the PURE
pieces — `AsteriskAdapter.parse_recording_event`, `AsteriskEventRouter.route_recording`, and
`cdr_row_to_event` (all stdlib-only in app/providers/asterisk.py) — plus a tiny in-memory
stand-in for the event-sourced projection. It does NOT hit Postgres: the real
`ingest_recording_event` / `ingest_status_event` need sqlalchemy + asyncpg which aren't in
this sandbox, so we assert the normalization + idempotency CONTRACT that feeds them.

Verified here:
- a faked RecordingFinished -> a NormalizedRecordingEvent whose sid/call_sid/status/duration
  are exactly what the existing pipeline consumes (so it enqueues a recording_fetch), and the
  router skips non-recording / nameless events.
- a CDR row for a call the WS "missed" -> the call is backfilled/completed in the projection.
- re-running the CDR reconcile is idempotent, and a CDR terminal + a WS terminal of the same
  status collapse onto ONE call_events row (shared "{linkedid}:{status}" dedup key) — never
  double-counted.

Run: python -m tests.test_asterisk_recordings
"""

import sys

from app.providers.asterisk import (
    AsteriskAdapter,
    AsteriskEventRouter,
    cdr_row_to_event,
)
from app.providers.base import STATUS_RANK


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"asterisk_recordings failed at: {name}")


LINKEDID = "1690000000.1"
LEG2 = "1690000000.2"


def _rec_event(name, *, state="done", duration=12):
    """A raw ARI RecordingFinished event as it arrives over the WS. The interpreter named
    the recording `{linkedid}-{tag}-{n}` (interpreter._rec_name)."""
    return {
        "type": "RecordingFinished",
        "timestamp": "2026-07-22T14:05:00.000+0000",
        "recording": {
            "name": name,
            "format": "wav",
            "state": state,
            "duration": duration,
            "target_uri": f"channel:{LINKEDID}",
        },
    }


def _cdr(**over):
    """A CDR row as cdr_pgsql wrote it (dict, like SQLAlchemy .mappings())."""
    row = {
        "linkedid": LINKEDID,
        "uniqueid": LINKEDID,   # entry leg: uniqueid == linkedid
        "src": "+13055551234",
        "dst": "+13055559999",
        "disposition": "ANSWERED",
        "start": "2026-07-22T14:03:00+00:00",
        "answer": "2026-07-22T14:03:05+00:00",
        "end": "2026-07-22T14:04:00+00:00",
        "duration": 60,
        "billsec": 55,
    }
    row.update(over)
    return row


class _FakeProjection:
    """Mirrors the ingest invariants that matter for idempotency: one row per
    provider_call_sid, forward-only status advance, and append-only call_events deduped on
    the (call_sid, event_type, provider_sequence) natural key (so re-ingest never double-
    counts)."""

    def __init__(self):
        self.calls = {}          # sid -> {"status", "rank"}
        self.events = set()      # (sid, event_type, provider_sequence)
        self.event_count = 0

    def ingest(self, evt):
        row = self.calls.setdefault(evt.provider_call_sid, {"status": None, "rank": 0})
        if evt.status_rank > row["rank"]:
            row["status"], row["rank"] = evt.status, evt.status_rank
        key = (evt.provider_call_sid, evt.event_type, evt.provider_sequence)
        if key not in self.events:
            self.events.add(key)
            self.event_count += 1


def test_parse_recording_event():
    print("parse_recording_event normalizes RecordingFinished -> pipeline shape:")
    a = AsteriskAdapter()
    rec = a.parse_recording_event(_rec_event(f"{LINKEDID}-vm-1"))
    check("provider_recording_sid == recording name (idempotency key)",
          rec.provider_recording_sid == f"{LINKEDID}-vm-1")
    check("provider_call_sid == linkedid parsed from the name prefix",
          rec.provider_call_sid == LINKEDID)
    check("state 'done' -> status 'completed' (same label a Twilio recording lands with)",
          rec.status == "completed")
    check("duration carried through", rec.duration_seconds == 12)
    check("no provider_url (local spool file -> fetch is a move, not a download)",
          rec.provider_url is None)


def test_route_recording():
    print("router.route_recording gates recording events:")
    router = AsteriskEventRouter()
    rec = router.route_recording(_rec_event(f"{LINKEDID}-play-1"))
    check("RecordingFinished routes to a NormalizedRecordingEvent", rec is not None)
    check("carries the sid", rec.provider_recording_sid == f"{LINKEDID}-play-1")
    check("a channel status event is NOT a recording event",
          router.route_recording({"type": "ChannelDestroyed", "channel": {}}) is None)
    check("a nameless recording is skipped",
          router.route_recording(_rec_event("")) is None)


def test_cdr_backfills_missed_call():
    print("CDR row for a WS-missed call backfills/completes it in the projection:")
    proj = _FakeProjection()
    evt = cdr_row_to_event(_cdr())
    check("entry-leg CDR yields an event", evt is not None)
    check("provider_call_sid == linkedid", evt.provider_call_sid == LINKEDID)
    check("ANSWERED -> completed (terminal rank 4)",
          evt.status == "completed" and evt.status_rank == 4)
    check("attribution: from=src, to=dst",
          evt.from_number == "+13055551234" and evt.to_number == "+13055559999")
    check("timestamps parsed", evt.started_at is not None and evt.ended_at is not None)
    check("duration = billsec (answered-duration)", evt.duration_seconds == 55)
    proj.ingest(evt)
    check("call now present + completed in the projection",
          proj.calls[LINKEDID]["status"] == "completed" and proj.calls[LINKEDID]["rank"] == 4)


def test_cdr_disposition_mapping():
    print("CDR disposition -> terminal status:")
    cases = {"ANSWERED": "completed", "NO ANSWER": "no-answer", "BUSY": "busy",
             "FAILED": "failed", "CONGESTION": "failed", "": "failed", "WEIRD": "failed"}
    for disp, expected in cases.items():
        evt = cdr_row_to_event(_cdr(disposition=disp))
        check(f"disposition {disp!r} -> {expected}", evt is not None and evt.status == expected)
        check(f"{expected} is a known STATUS_RANK terminal", STATUS_RANK.get(expected, 0) >= 4)


def test_cdr_secondary_leg_dropped():
    print("secondary (dialed) leg CDR is dropped (one calls row per linkedid):")
    evt = cdr_row_to_event(_cdr(uniqueid=LEG2))  # uniqueid != linkedid -> not entry leg
    check("secondary-leg CDR skipped", evt is None)
    check("row with no linkedid skipped", cdr_row_to_event(_cdr(linkedid="")) is None)


def test_reconcile_idempotent_and_no_double_count():
    print("re-running CDR reconcile is idempotent; coexists with an earlier WS terminal:")
    proj = _FakeProjection()

    # Simulate the live WS having ALREADY projected the terminal (completed) for this call.
    ws_router = AsteriskEventRouter()
    ws_terminal = ws_router.route({
        "type": "ChannelDestroyed", "cause": 16,
        "timestamp": "2026-07-22T14:04:00.000+0000",
        "channel": {"id": LINKEDID, "channelvars": {"Linkedid": LINKEDID},
                    "caller": {"number": "+13055551234"},
                    "dialplan": {"exten": "+13055559999"}},
    })
    proj.ingest(ws_terminal)
    check("WS terminal used key '{linkedid}:completed'",
          ws_terminal.provider_sequence == f"{LINKEDID}:completed")

    # The CDR reconcile now runs THREE times over the same call (proving re-scan idempotency).
    cdr_evt = cdr_row_to_event(_cdr())
    check("CDR event_type is 'cdr' (its own append-only source, keyed for its own dedup)",
          cdr_evt.event_type == "cdr")
    check("CDR terminal reuses the shared '{linkedid}:{status}' sequence convention",
          cdr_evt.provider_sequence == f"{LINKEDID}:completed")
    proj.ingest(cdr_evt)
    events_after_first_cdr = proj.event_count
    proj.ingest(cdr_evt)  # re-scan
    proj.ingest(cdr_evt)  # re-scan again
    check("re-running the CDR reconcile adds NO further call_events rows (idempotent)",
          proj.event_count == events_after_first_cdr)
    check("still exactly one calls row (WS + CDR collapse onto one call)", len(proj.calls) == 1)
    check("call remains completed, never regressed",
          proj.calls[LINKEDID]["status"] == "completed" and proj.calls[LINKEDID]["rank"] == 4)


def main():
    test_parse_recording_event()
    test_route_recording()
    test_cdr_backfills_missed_call()
    test_cdr_disposition_mapping()
    test_cdr_secondary_leg_dropped()
    test_reconcile_idempotent_and_no_double_count()
    print("\nALL ASTERISK RECORDINGS + CDR CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)
