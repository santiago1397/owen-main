"""Quo call recordings: the real response shape, every caller, and the repair of the 499.

MEASURED ON PRODUCTION 2026-09-14 (read-only): `GET /v1/call-recordings/{callId}` answers

    calls with audio:    {"data": [ {"duration", "id", "startTime", "status", "type", "url"} ]}
    calls without audio: {"data": []}

The client returned that LIST as if it were one dict, so the poll marked every call as having
no recording (all 499 mirrored calls reached the CRM with `recording_url = NULL`) and the
stream endpoint would have answered 500. Every test below uses those exact shapes and asserts
what came out — the CRM body, the bytes, the queued jobs, the requests on the wire:

  1. `pick_recording`: list with one, list empty, several, a bare dict, garbage.
  2. The poll: list-with-one -> the CRM body carries `recording_url`; list-empty -> it does
     not, and nothing errors.
  3. The stream endpoint: list shape -> audio bytes (the COMPLETED recording when there are
     several); empty -> 404 with a sentence; a shape surprise -> 404, never a 500.
  4. The webhook: `call.completed` asks for the recording and gets it right;
     `call.recording.completed` sets it too.
  5. `manage.py recordings`: a dry run enqueues nothing, `--commit` enqueues only calls with
     audio under the call's own dedupe key, staggered; a second commit enqueues nothing; and
     zero non-GET requests reach Quo.

Stdlib only apart from the app itself, like every other test here.
Run: python -m tests.test_openphone_recordings
"""

import asyncio
import re

from tests.test_openphone_mirror import (A_CALL, CUSTOMER, OUR_LINE, OUR_LINE_ID, FakeResult,
                                         FakeSession, RecordedResponse, RecordingTransport,
                                         account, check, run_mirror)

REC_URL = "https://share.quo.com/rec/abc.mp3"


def rec(url=REC_URL, status="completed", duration=184, rid="REC1"):
    """One entry exactly as Quo lists it."""
    return {"duration": duration, "id": rid, "startTime": "2026-09-11T10:00:05Z",
            "status": status, "type": "audio/mpeg", "url": url}


WITH_ONE = {"data": [rec()]}
EMPTY = {"data": []}


def _crm_bodies(jobs):
    from app.integrations.openphone.events import to_crm_event

    return [to_crm_event(p["body"]) for t, p in jobs if t == "crm_report"]


# ══════════════════════════════════════════════════════════════════════════════════════════
#  1. The one function every caller goes through
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_pick_recording_reads_the_measured_shapes():
    print("pick_recording:")
    from app.providers.openphone_client import pick_recording

    check("list with one -> that recording", pick_recording(WITH_ONE)["url"] == REC_URL)
    check("list empty -> {}", pick_recording(EMPTY) == {})
    several = {"data": [rec("https://share.quo.com/long-but-failed.mp3", "failed", 900, "A"),
                        rec("https://share.quo.com/short-done.mp3", "completed", 30, "B"),
                        rec("https://share.quo.com/long-done.mp3", "completed", 200, "C"),
                        {"id": "D", "status": "completed", "duration": 999}]}
    check("several -> a completed one with a url, the longest of those",
          pick_recording(several)["id"] == "C")
    check("several, none completed -> the longest with a url",
          pick_recording({"data": [rec(status="processing", duration=5, rid="X"),
                                   rec(status="processing", duration=50, rid="Y")]})["id"]
          == "Y")
    check("a bare dict is still accepted", pick_recording(rec())["url"] == REC_URL)
    for junk in (None, "", "nope", {"data": "nope"}, {"data": [None, 3, {"url": ""}]}, []):
        check(f"{junk!r} -> {{}}", pick_recording(junk) == {})


def test_get_call_recording_returns_a_dict_from_the_list_shape():
    print("get_call_recording over the wire:")
    from app.core.config import settings
    from app.providers import openphone_client as op_client

    for payload, want in ((WITH_ONE, REC_URL), (EMPTY, None)):
        transport = RecordingTransport({r"/call-recordings/": payload})
        saved = (settings.OPENPHONE_API_KEY, op_client.httpx.AsyncClient)
        try:
            settings.OPENPHONE_API_KEY = "op_test_key"
            op_client.httpx.AsyncClient = lambda *a, **kw: transport
            got = asyncio.run(op_client.get_call_recording("AC_call_1"))
        finally:
            settings.OPENPHONE_API_KEY, op_client.httpx.AsyncClient = saved
        check(f"a dict, url={want!r} ({got})", isinstance(got, dict) and got.get("url") == want)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  2. The poll
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_the_poll_sends_recording_url_for_a_call_with_audio():
    print("poll, recording list with one entry:")
    routes = account(calls=[A_CALL])
    routes[r"/call-recordings/"] = WITH_ONE
    result, transport, _ = run_mirror(routes)
    bodies = _crm_bodies(result["_jobs"])
    check("one call mirrored", len(bodies) == 1)
    check("it asked Quo for the recording",
          any("/call-recordings/AC_call_1" in c["url"] for c in transport.calls))
    check("the CRM body carries recording_url at the CRM's own route",
          bodies[0].get("recording_url") == "/api/openphone/recordings/AC_call_1")
    check("no error recorded", result["errors"] == [])


def test_the_poll_sends_no_recording_url_for_a_call_without_audio():
    print("poll, recording list empty:")
    routes = account(calls=[A_CALL])
    routes[r"/call-recordings/"] = EMPTY
    result, _, _ = run_mirror(routes)
    bodies = _crm_bodies(result["_jobs"])
    check("the call is still mirrored", len(bodies) == 1)
    check("no recording_url", "recording_url" not in bodies[0])
    check("no error recorded", result["errors"] == [] and result["ran"] is True)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  3. The stream endpoint the CRM calls
# ══════════════════════════════════════════════════════════════════════════════════════════

def stream(payload, *, fail=False):
    """Call the REAL route function. Returns `(status, detail_or_bytes, transport)`."""
    from fastapi import HTTPException

    from app.core.config import settings
    from app.integrations.openphone import api
    from app.providers import openphone_client as op_client

    class AudioTransport(RecordingTransport):
        async def get(self, url, params=None, headers=None, **kw):
            self._record("GET", url, params=params)
            if "share.quo.com" in url:
                return RecordedResponse({}, content=b"ID3-" + url.encode())
            if fail:
                raise RuntimeError("HTTP 503")
            return RecordedResponse(payload)

    transport = AudioTransport({})
    saved = (settings.OPENPHONE_MIRROR_ENABLED, settings.OPENPHONE_API_KEY,
             op_client.httpx.AsyncClient)
    try:
        settings.OPENPHONE_MIRROR_ENABLED = True
        settings.OPENPHONE_API_KEY = "op_test_key"
        op_client.httpx.AsyncClient = lambda *a, **kw: transport
        try:
            resp = asyncio.run(api.stream_recording("AC_call_1", _key=None))
            return resp.status_code, resp.body, transport
        except HTTPException as exc:
            return exc.status_code, exc.detail, transport
    finally:
        (settings.OPENPHONE_MIRROR_ENABLED, settings.OPENPHONE_API_KEY,
         op_client.httpx.AsyncClient) = saved


def test_the_stream_endpoint_returns_audio_for_the_list_shape():
    print("stream, list with one:")
    status, body, transport = stream(WITH_ONE)
    check(f"200 ({status})", status == 200)
    check("the bytes are the recording Quo listed", body == b"ID3-" + REC_URL.encode())
    check("every request was a GET", all(c["method"] == "GET" for c in transport.calls))


def test_the_stream_endpoint_picks_the_completed_recording_of_several():
    print("stream, several recordings:")
    status, body, _ = stream({"data": [
        rec("https://share.quo.com/partial.mp3", "failed", 400, "A"),
        rec("https://share.quo.com/final.mp3", "completed", 184, "B")]})
    check("200", status == 200)
    check("the completed one was streamed", body == b"ID3-https://share.quo.com/final.mp3")


def test_the_stream_endpoint_is_404_with_a_sentence_when_there_is_no_audio():
    print("stream, list empty and shape surprises:")
    status, detail, transport = stream(EMPTY)
    check(f"404 ({status})", status == 404)
    check(f"a sentence ({detail!r})", isinstance(detail, str) and "no recording" in detail)
    check("no media fetch was attempted",
          not any("share.quo.com" in c["url"] for c in transport.calls))
    for surprise in ({"data": "what"}, {"data": [{"id": "no-url"}]}, [], None):
        status, detail, _ = stream(surprise)
        check(f"{surprise!r} -> 404 with a sentence, not a 500 ({status})",
              status == 404 and isinstance(detail, str) and detail)
    status, detail, _ = stream(None, fail=True)
    check(f"Quo unreachable -> 502 with a sentence ({status})",
          status == 502 and "could not be reached" in detail)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  4. The webhook
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_the_webhook_sets_recording_url_from_the_list_shape():
    print("webhook call.completed and call.recording.completed:")
    from tests.test_openphone_webhook import CALL, configured, process, queued_body

    for recordings, want in ((WITH_ONE, True), (EMPTY, False)):
        routes = {r"/calls/AC_call_1": {"data": CALL}, **account(calls=[CALL])}
        routes[r"/call-recordings/"] = recordings
        calls = []
        with configured():
            result, jobs = process(queued_body("call.completed", CALL), routes,
                                   FakeSession(), calls)
        from app.integrations.openphone.events import to_crm_event
        body = to_crm_event(jobs[0]["body"])
        check(f"call.completed, recordings={len(recordings['data'])}: outcome sent, "
              f"recording_url {'present' if want else 'absent'}",
              result.get("outcome") == "sent"
              and (body.get("recording_url") == "/api/openphone/recordings/AC_call_1") == want)
        check("  and every Quo request was a GET",
              all(c["method"] == "GET" for c in calls if "api.openphone.com" in c["url"]))

    # The recording finishes after the call was mirrored without one.
    session = FakeSession()
    routes = {r"/calls/AC_call_1": {"data": CALL}, **account(calls=[CALL])}
    routes[r"/call-recordings/"] = EMPTY
    with configured():
        process(queued_body("call.completed", CALL), routes, session)
        routes[r"/call-recordings/"] = WITH_ONE
        media = dict(CALL, media=[{"url": REC_URL, "type": "audio/mpeg"}])
        result, jobs = process(queued_body("call.recording.completed", media), routes, session)
    from app.integrations.openphone.events import to_crm_event
    body = to_crm_event(jobs[0]["body"])
    check(f"call.recording.completed afterwards: sent ({result})",
          result.get("outcome") == "sent")
    check("under the call's own dedupe key, with recording_url",
          body["dedupe_key"] == "openphone:call:AC_call_1"
          and body.get("recording_url") == "/api/openphone/recordings/AC_call_1")


# ══════════════════════════════════════════════════════════════════════════════════════════
#  5. The repair command
# ══════════════════════════════════════════════════════════════════════════════════════════

class Row:
    def __init__(self, call_id, key="9415550123", line=OUR_LINE):
        self.external_id, self.customer_key, self.line_number = call_id, key, line
        self.occurred_at = None

    def __getitem__(self, i):
        return (self.external_id, self.customer_key, self.line_number, self.occurred_at)[i]


class RepairSession(FakeSession):
    """FakeSession, plus the one listing query the repair makes."""

    def __init__(self, call_ids):
        super().__init__(mirrored={("call", c) for c in call_ids})
        self.rows = [Row(c) for c in call_ids]

    async def execute(self, stmt):
        text = str(stmt)
        if "openphone_mirror_rows" in text and "customer_key" in text.split("FROM")[0]:
            return FakeResult(self.rows)
        return await super().execute(stmt)


# Three already-mirrored calls: two with audio (one of them with several recordings), one
# without — the same 3-of-5 / 2-of-5 mix production showed, in miniature.
REPAIR_ROUTES = {
    r"/call-recordings/AC_a$": WITH_ONE,
    r"/call-recordings/AC_b$": EMPTY,
    r"/call-recordings/AC_c$": {"data": [rec(status="failed", rid="x"), rec(rid="y")]},
    r"/calls/AC_(a|b|c)$": {"data": {"id": "AC_x", "direction": "outgoing",
                                     "status": "completed", "from": OUR_LINE,
                                     "to": CUSTOMER, "phoneNumberId": OUR_LINE_ID,
                                     "createdAt": "2026-09-10T15:00:00Z",
                                     "answeredAt": "2026-09-10T15:00:04Z",
                                     "completedAt": "2026-09-10T15:01:04Z"}},
}


def repair(session, *, commit, transport_calls, key="op_test_key", agent="owen_sk_test"):
    from app.core.config import settings
    from app.integrations.openphone import push as op_push
    from app.integrations.openphone import recordings
    from app.providers import openphone_client as op_client
    from app.services import queue as queue_mod

    transport = RecordingTransport(REPAIR_ROUTES)
    jobs, sleeps = [], []

    async def fake_enqueue(db, job_type, payload, delay_seconds=0):
        jobs.append((job_type, payload, delay_seconds))
        await db.commit()

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    saved = (settings.OPENPHONE_MIRROR_ENABLED, settings.OPENPHONE_API_KEY,
             settings.AGENT_RUNTIME_KEY, op_client.httpx.AsyncClient, queue_mod.enqueue,
             op_push.queue.enqueue)
    try:
        settings.OPENPHONE_MIRROR_ENABLED = True
        settings.OPENPHONE_API_KEY = key
        settings.AGENT_RUNTIME_KEY = agent
        op_client.httpx.AsyncClient = lambda *a, **kw: transport
        queue_mod.enqueue = fake_enqueue
        op_push.queue.enqueue = fake_enqueue
        result = asyncio.run(recordings.repair(commit=commit, session_factory=lambda: session,
                                               sleep=fake_sleep))
    finally:
        (settings.OPENPHONE_MIRROR_ENABLED, settings.OPENPHONE_API_KEY,
         settings.AGENT_RUNTIME_KEY, op_client.httpx.AsyncClient, queue_mod.enqueue,
         op_push.queue.enqueue) = saved
    transport_calls.extend(transport.calls)
    return result, jobs, sleeps


def test_the_repair_dry_run_counts_and_enqueues_nothing():
    print("manage.py recordings (dry run):")
    session, calls = RepairSession(["AC_a", "AC_b", "AC_c"]), []
    result, jobs, _ = repair(session, commit=False, transport_calls=calls)
    print(f"       {result}")
    check("it checked all three", result["checked"] == 3)
    check("two with audio, one without",
          result["with_audio"] == 2 and result["without_audio"] == 1)
    check("nothing enqueued", result["enqueued"] == 0 and jobs == [])
    check("no state row written", session.added == [])
    check("no call detail fetched on a dry run",
          not any(re.search(r"/calls/AC_", c["url"]) for c in calls))


def test_the_repair_commit_enqueues_only_calls_with_audio_and_a_rerun_nothing():
    print("manage.py recordings --commit, twice:")
    session, calls = RepairSession(["AC_a", "AC_b", "AC_c"]), []
    first, jobs, sleeps = repair(session, commit=True, transport_calls=calls)
    print(f"       {first}")
    check("enqueued exactly the two with audio", first["enqueued"] == 2 and len(jobs) == 2)
    check("no errors", first["errors"] == 0)
    bodies = [(p["body"], delay) for _, p, delay in jobs]
    ids = sorted(b["external_id"] for b, _ in bodies)
    check(f"AC_a and AC_c, never AC_b ({ids})", ids == ["AC_a", "AC_c"])

    from app.integrations.openphone.events import to_crm_event
    for body, _ in bodies:
        crm = to_crm_event(body)
        check(f"{body['external_id']}: the call's SAME dedupe key, so the CRM fills its row",
              crm["dedupe_key"] == "openphone:call:" + body["external_id"])
        check("  carries recording_url at the CRM's route",
              crm["recording_url"] == "/api/openphone/recordings/" + body["external_id"])
        check("  real facts from GET /calls/{id}: OUTBOUND, completed, 60s",
              crm["direction"] == "OUTBOUND" and crm["call_status"] == "completed"
              and crm["duration_seconds"] == 60)
        check("  the customer, not our line", crm["from_number"] == CUSTOMER)
    check(f"deliveries are staggered, not a burst ({[d for _, d in bodies]})",
          [d for _, d in bodies] == [0, 3])
    check(f"Quo requests are paced ({sleeps})", sleeps and all(s >= 0.1 for s in sleeps))
    check("the state rows are the webhook's recording-part kind",
          {(o.kind, o.external_id) for o in session.added if hasattr(o, "kind")}
          == {("call_recording", "AC_a"), ("call_recording", "AC_c")})

    second, jobs2, _ = repair(session, commit=True, transport_calls=calls)
    print(f"       {second}")
    check("a second commit enqueues nothing", second["enqueued"] == 0 and jobs2 == [])
    check("  and says both were already sent", second["already_sent"] == 2)

    openphone = [c for c in calls if "api.openphone.com" in c["url"]]
    check(f"requests were made ({len(openphone)})", len(openphone) > 0)
    check("ZERO non-GET requests to Quo, across both runs",
          [c for c in calls if c["method"] != "GET"] == [])


def test_the_repair_counts_a_failed_lookup_and_goes_on():
    print("repair with one lookup failing:")
    from app.providers import openphone_client as op_client

    real = op_client.get_call_recording

    async def flaky(call_id):
        if call_id == "AC_a":
            raise RuntimeError("HTTP 429")
        return await real(call_id)

    session, calls = RepairSession(["AC_a", "AC_c"]), []
    op_client.get_call_recording = flaky
    try:
        result, jobs, _ = repair(session, commit=True, transport_calls=calls)
    finally:
        op_client.get_call_recording = real
    print(f"       {result}")
    check("one error counted", result["errors"] == 1)
    check("the other call was still repaired", result["enqueued"] == 1 and len(jobs) == 1)
    check("the failed call has no state row, so the next run retries it",
          ("call_recording", "AC_a") not in session.mirrored)


def test_the_repair_refuses_without_the_keys_and_makes_no_request():
    print("repair, unconfigured:")
    calls = []
    result, jobs, _ = repair(RepairSession(["AC_a"]), commit=False, transport_calls=calls,
                             key="")
    check(f"declined ({result.get('reason')})", result["ran"] is False and not jobs)
    check("no request made", calls == [])
    result, jobs, _ = repair(RepairSession(["AC_a"]), commit=True, transport_calls=calls,
                             agent="")
    check(f"--commit without AGENT_RUNTIME_KEY declined ({result.get('reason')})",
          result["ran"] is False and not jobs and calls == [])


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        print(f"\n{fn.__name__}")
        fn()
    print(f"\nAll {len(tests)} OpenPhone-recording checks passed.")


if __name__ == "__main__":
    main()
