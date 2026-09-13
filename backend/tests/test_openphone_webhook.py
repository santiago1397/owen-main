"""The Quo webhook: authentic deliveries only, one CRM event per object, and still no writes.

`app/integrations/openphone/webhook.py` is a PUBLIC route on a live phone system. Every
test here asserts what was — and was not — WRITTEN, never a status code alone:

  1. A valid signature is accepted; a missing, malformed, wrong or stale one is refused
     and nothing is enqueued. The scheme is Quo's documented one, and the test computes
     its digests independently of the module rather than calling the module's helper.
  2. The kill switch off means 404 before the body is even read.
  3. A webhook and the 5-minute poll delivering the same object produce ONE CRM job, in
     either order.
  4. A call's later parts (recording, transcript, summary) are each sent once, under the
     call's own CRM dedupe key, so the CRM fills in its one row.
  5. Processing issues no non-GET request to api.openphone.com.
  6. Quo's contact name rides along for the CRM's number-only thread.

Stdlib only apart from the app itself, like every other test here.
Run: python -m tests.test_openphone_webhook
"""

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import time

from tests.test_openphone_mirror import (CUSTOMER, OUR_LINE, OUR_LINE_ID, FakeSession,
                                         RecordingTransport, account, check, run_mirror)

SECRET = base64.b64encode(b"a-thirty-two-byte-signing-key!!!").decode()


def sign(raw: bytes, *, secret: str = SECRET, ts_ms: int | None = None) -> str:
    """Quo's documented scheme, written out here rather than imported."""
    ts = str(int(time.time() * 1000) if ts_ms is None else ts_ms)
    key = base64.b64decode(secret)
    digest = base64.b64encode(
        hmac.new(key, ts.encode() + b"." + raw, hashlib.sha256).digest()).decode()
    return f"hmac;1;{ts};{digest}"


def event(kind: str, obj: dict, event_id: str = "EV1") -> bytes:
    return json.dumps({"id": event_id, "object": "event", "apiVersion": "v3",
                       "createdAt": "2026-09-13T10:00:00Z", "type": kind,
                       "data": {"object": obj}}, separators=(",", ":")).encode()


MESSAGE = {"id": "AC_msg_1", "object": "message", "from": CUSTOMER, "to": OUR_LINE,
           "direction": "incoming", "body": "Can you come Tuesday?", "media": [],
           "status": "received", "createdAt": "2026-09-11T10:05:00Z",
           "phoneNumberId": OUR_LINE_ID, "conversationId": "CN1"}

CALL = {"id": "AC_call_1", "object": "call", "from": CUSTOMER, "to": OUR_LINE,
        "direction": "incoming", "media": [], "voicemail": None, "status": "completed",
        "createdAt": "2026-09-11T10:00:00Z", "answeredAt": "2026-09-11T10:00:05Z",
        "completedAt": "2026-09-11T10:03:09Z", "phoneNumberId": OUR_LINE_ID}


@contextlib.contextmanager
def configured(*, webhook=True, mirror=True, secret=SECRET, api_key="op_test_key"):
    from app.core.config import settings

    names = ("OPENPHONE_WEBHOOK_ENABLED", "OPENPHONE_WEBHOOK_SECRET",
             "OPENPHONE_MIRROR_ENABLED", "OPENPHONE_API_KEY", "AGENT_RUNTIME_KEY")
    saved = {n: getattr(settings, n) for n in names}
    try:
        settings.OPENPHONE_WEBHOOK_ENABLED = webhook
        settings.OPENPHONE_WEBHOOK_SECRET = secret
        settings.OPENPHONE_MIRROR_ENABLED = mirror
        settings.OPENPHONE_API_KEY = api_key
        settings.AGENT_RUNTIME_KEY = "owen_sk_test"
        yield settings
    finally:
        for n, v in saved.items():
            setattr(settings, n, v)


def accept(raw, header, **kw):
    from app.integrations.openphone import webhook

    jobs = []

    async def fake_enqueue(body):
        jobs.append(body)

    status, body = asyncio.run(webhook.accept(raw, header, enqueue=fake_enqueue, **kw))
    return status, body, jobs


# ══════════════════════════════════════════════════════════════════════════════════════════
#  1. Only an authentic delivery is accepted
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_a_valid_signature_is_accepted_and_queues_one_job():
    print("a correctly signed message.received:")
    raw = event("message.received", MESSAGE)
    with configured():
        status, body, jobs = accept(raw, sign(raw))
    check(f"200 ({status} {body})", status == 200)
    check("exactly one job queued", len(jobs) == 1)
    check("it carries the event type and the object",
          jobs[0]["type"] == "message.received" and jobs[0]["object"]["id"] == "AC_msg_1")


def test_the_node_samples_compact_json_form_is_accepted_too():
    print("signed over JSON.stringify(body), delivered pretty-printed:")
    obj = json.loads(event("message.received", MESSAGE))
    pretty = json.dumps(obj, indent=2).encode()
    compact = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    with configured():
        status, _, jobs = accept(pretty, sign(compact))
    check("accepted", status == 200 and len(jobs) == 1)


def test_every_inauthentic_delivery_is_refused_and_writes_nothing():
    print("missing / malformed / wrong / tampered / stale / future:")
    raw = event("message.received", MESSAGE)
    other = base64.b64encode(b"somebody-else's-key-entirely!!!!").decode()
    now = int(time.time() * 1000)
    cases = {
        "missing header": None,
        "empty header": "",
        "malformed (three fields)": "hmac;1;%d" % now,
        "wrong scheme": sign(raw).replace("hmac;", "sha1;", 1),
        "non-numeric timestamp": "hmac;1;yesterday;abc=",
        "wrong key": sign(raw, secret=other),
        "tampered body": sign(raw.replace(b"Tuesday", b"Wednesday")),
        "stale by 301s": sign(raw, ts_ms=now - 301_000),
        "future by 301s": sign(raw, ts_ms=now + 301_000),
    }
    with configured():
        for label, header in cases.items():
            status, body, jobs = accept(raw, header)
            check(f"{label}: 401 and nothing queued", status == 401 and jobs == [])
            check(f"{label}: the response echoes no secret",
                  SECRET not in json.dumps(body))
        status, _, jobs = accept(raw, sign(raw, ts_ms=now - 299_000))
        check("inside the window (299s old) is still accepted", status == 200 and len(jobs) == 1)


def test_no_secret_configured_refuses_and_writes_nothing():
    print("OPENPHONE_WEBHOOK_SECRET empty:")
    raw = event("message.received", MESSAGE)
    with configured(secret=""):
        status, _, jobs = accept(raw, sign(raw))
    check("503 and nothing queued", status == 503 and jobs == [])
    with configured(secret="!!!not base64!!!"):
        status, _, jobs = accept(raw, sign(raw))
    check("a secret that is not base64 refuses every delivery", status == 401 and jobs == [])


def test_the_kill_switch_off_answers_404_without_reading_the_body():
    print("OPENPHONE_WEBHOOK_ENABLED=false:")
    from app.integrations.openphone import webhook

    read = []
    queued = []

    class FakeRequest:
        headers = {"openphone-signature": "hmac;1;1;x"}

        async def body(self):
            read.append(True)
            return b"{}"

    async def fake_enqueue(body):
        queued.append(body)

    saved = webhook._enqueue
    webhook._enqueue = fake_enqueue
    try:
        with configured(webhook=False):
            resp = asyncio.run(webhook.receive(FakeRequest()))
    finally:
        webhook._enqueue = saved
    check("404", resp.status_code == 404)
    check("the body was never read", read == [])
    check("nothing was queued", queued == [])


def test_an_unknown_event_type_is_acknowledged_and_ignored():
    print("call.ringing / contact.updated / task.created:")
    with configured():
        for kind in ("call.ringing", "contact.updated", "task.created"):
            raw = event(kind, {"id": "X"})
            status, body, jobs = accept(raw, sign(raw))
            check(f"{kind}: 200 ignored, nothing queued",
                  status == 200 and body.get("ignored") == kind and jobs == [])


def test_a_mirror_that_is_switched_off_refuses_rather_than_queueing():
    print("webhook on, mirror off:")
    raw = event("message.received", MESSAGE)
    with configured(mirror=False):
        status, _, jobs = accept(raw, sign(raw))
    check("503 so Quo retries later, and nothing queued", status == 503 and jobs == [])


# ══════════════════════════════════════════════════════════════════════════════════════════
#  2. Processing: one object, one CRM event, however it arrives
# ══════════════════════════════════════════════════════════════════════════════════════════

def process(body, routes, session, transport_out=None):
    """Drive `webhook.process` against a recording transport and a shared session."""
    import httpx

    from app.integrations.openphone import push as op_push
    from app.integrations.openphone import webhook
    from app.providers import openphone_client as op_client
    from app.services import queue as queue_mod

    transport = RecordingTransport(routes)
    jobs = []

    async def fake_enqueue(db, job_type, payload, delay_seconds=0):
        jobs.append((job_type, payload))
        await db.commit()

    saved = (httpx.AsyncClient, op_client.httpx.AsyncClient, queue_mod.enqueue,
             op_push.queue.enqueue)
    try:
        httpx.AsyncClient = lambda *a, **kw: transport
        op_client.httpx.AsyncClient = lambda *a, **kw: transport
        queue_mod.enqueue = fake_enqueue
        op_push.queue.enqueue = fake_enqueue
        result = asyncio.run(webhook.process(body, session_factory=lambda: session))
    finally:
        (httpx.AsyncClient, op_client.httpx.AsyncClient, queue_mod.enqueue,
         op_push.queue.enqueue) = saved
    if transport_out is not None:
        transport_out.extend(transport.calls)
    return result, [p for t, p in jobs if t == "crm_report"]


def queued_body(kind, obj):
    return {"source": "webhook", "event_id": "EV", "type": kind, "object": obj}


def test_webhook_then_poll_is_one_crm_event():
    print("the same text by webhook, then by the poll:")
    session = FakeSession()
    with configured():
        result, jobs = process(queued_body("message.received", MESSAGE),
                               account(), session)
    check(f"the webhook mirrored it ({result})", result.get("outcome") == "sent")
    poll, _, _ = run_mirror(account(messages=[dict(MESSAGE, text=MESSAGE["body"])]),
                            session=session)
    poll_jobs = [p for t, p in poll["_jobs"] if t == "crm_report"]
    check("the poll found it already mirrored", poll["duplicates"] >= 1)
    check(f"ONE CRM job across both (got {len(jobs) + len(poll_jobs)})",
          len(jobs) + len(poll_jobs) == 1)


def test_poll_then_webhook_is_one_crm_event():
    print("the same text by the poll, then by webhook:")
    session = FakeSession()
    poll, _, _ = run_mirror(account(messages=[dict(MESSAGE, text=MESSAGE["body"])]),
                            session=session)
    with configured():
        result, jobs = process(queued_body("message.received", MESSAGE), account(),
                               session)
    check("the webhook said duplicate", result.get("outcome") == "duplicate")
    check("and queued nothing", jobs == [])
    check("the poll queued the one", len([t for t, _ in poll["_jobs"]]) == 1)


def test_a_calls_later_parts_each_go_once_under_the_calls_own_key():
    print("call.completed, then recording, transcript, summary — each twice:")
    from app.integrations.openphone.events import to_crm_event

    session = FakeSession()
    calls = []
    all_jobs = []
    # The by-id route goes FIRST: the transport matches patterns in order, and
    # account()'s `/calls\b` would otherwise answer a `/calls/<id>` read with a list.
    routes = {r"/calls/AC_call_1": {"data": CALL}, **account(calls=[CALL])}
    # At call.completed time Quo has not produced these yet.
    routes[r"/call-transcripts/"] = {"dialogue": []}
    routes[r"/call-summaries/"] = {"summary": ""}
    routes[r"/call-recordings/"] = {}
    with configured():
        _, jobs = process(queued_body("call.completed", CALL), routes, session, calls)
        all_jobs += jobs
        parts = [
            ("call.recording.completed",
             dict(CALL, media=[{"url": "https://share.quo.com/r.mp3", "type": "audio/mpeg"}])),
            ("call.transcript.completed",
             {"object": "callTranscript", "callId": "AC_call_1", "status": "completed",
              "dialogue": [{"identifier": CUSTOMER, "content": "The skylight leaks."}]}),
            ("call.summary.completed",
             {"object": "callSummary", "callId": "AC_call_1", "status": "completed",
              "summary": ["Skylight leak, wants a visit."], "nextSteps": []}),
        ]
        for kind, obj in parts * 2:
            _, jobs = process(queued_body(kind, obj), routes, session, calls)
            all_jobs += jobs

    check(f"four CRM jobs: the call and three parts, none repeated (got {len(all_jobs)})",
          len(all_jobs) == 4)
    bodies = [to_crm_event(j["body"]) for j in all_jobs]
    check("every one carries the CALL's own dedupe key",
          {b["dedupe_key"] for b in bodies} == {"openphone:call:AC_call_1"})
    check("the first is the call, answered: 184s talk, completed",
          bodies[0]["duration_seconds"] == 184 and bodies[0]["call_status"] == "completed")
    check("the recording part points the CRM at its own recording route",
          any(b.get("recording_url") == "/api/openphone/recordings/AC_call_1"
              for b in bodies[1:]))
    check("the transcript part carries the words",
          any("skylight leaks" in (b.get("transcript") or "").lower() for b in bodies[1:]))
    check("the summary part carries the summary for the CRM to append once",
          any(b.get("summary") == "Skylight leak, wants a visit." for b in bodies[1:]))
    openphone = [c for c in calls if "api.openphone.com" in c["url"]]
    check(f"requests were made ({len(openphone)})", len(openphone) > 0)
    check("ZERO non-GET requests to api.openphone.com",
          [c for c in openphone if c["method"] != "GET"] == [])


def test_an_unanswered_call_with_a_voicemail_is_reported_as_voicemail():
    print("Quo's own example: status completed, never answered, voicemail present:")
    from app.integrations.openphone import webhook

    entry = webhook.call_entry(dict(CALL, answeredAt=None,
                                    voicemail={"url": "u", "duration": 7}))
    check("voicemail, not completed", entry["status"] == "voicemail")
    check("no talk time", entry["duration"] == 0)
    check("a recording exists", entry["hasRecording"] is True)
    missed = webhook.call_entry(dict(CALL, answeredAt=None))
    check("no voicemail and never answered is no-answer", missed["status"] == "no-answer")


def test_a_text_on_a_line_we_do_not_mirror_is_skipped():
    print("an excluded line:")
    from app.core.config import settings

    session = FakeSession()
    saved = settings.OPENPHONE_MIRROR_EXCLUDE_NUMBERS
    try:
        settings.OPENPHONE_MIRROR_EXCLUDE_NUMBERS = OUR_LINE
        with configured():
            result, jobs = process(queued_body("message.received", MESSAGE), account(),
                                   session)
    finally:
        settings.OPENPHONE_MIRROR_EXCLUDE_NUMBERS = saved
    check("skipped, nothing queued", result.get("skipped") and jobs == [])


# ══════════════════════════════════════════════════════════════════════════════════════════
#  3. Quo's name, the surface, and the write fence
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_quos_contact_name_rides_along_for_a_number_the_crm_does_not_know():
    print("Quo's contact name:")
    from app.integrations.openphone import contact_book
    from app.integrations.openphone.events import MirroredMessage, to_crm_event

    contact_book.forget()
    contact_book.remember([{"defaultFields": {
        "firstName": "Bob", "lastName": "Builder",
        "phoneNumbers": [{"value": "(941) 555-0123"}]}}])
    name = asyncio.run(contact_book.name_for(CUSTOMER))
    check(f"found by the last ten digits ({name!r})", name == "Bob Builder")
    body = to_crm_event(MirroredMessage(external_id="M", customer_number=CUSTOMER,
                                        contact_name=name).as_payload())
    check("sent as source_contact_name", body.get("source_contact_name") == "Bob Builder")
    plain = to_crm_event(MirroredMessage(external_id="M", customer_number=CUSTOMER)
                         .as_payload())
    check("absent when Quo has no name", "source_contact_name" not in plain)
    contact_book.forget()


def test_the_public_route_is_mounted_where_the_owner_is_told_it_is():
    print("the public surface:")
    import app.main as main_mod
    from app.integrations.openphone import webhook

    paths = {(getattr(r, "path", ""), tuple(sorted(getattr(r, "methods", []) or [])))
             for r in main_mod.app.routes}
    check("POST /webhooks/openphone is mounted", ("/webhooks/openphone", ("POST",)) in paths)
    check("and it is the documented path", webhook.WEBHOOK_PATH == "/webhooks/openphone")
    check("no GET or other verb is exposed there",
          not any(p == "/webhooks/openphone" and m != ("POST",) for p, m in paths))


def test_the_receiver_module_has_no_way_to_write_to_openphone():
    print("webhook.py and contact_book.py, read as source:")
    import ast
    import pathlib

    import app.integrations.openphone as pkg

    root = pathlib.Path(pkg.__file__).parent
    for name in ("webhook.py", "contact_book.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        calls = [n.func.attr for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == "op"]
        writes = [c for c in calls if not c.startswith(("list_", "get_"))]
        check(f"{name}: every OpenPhone call is a list_/get_ read ({sorted(set(calls))})",
              writes == [])
        check(f"{name}: never mentions registering a webhook",
              "/webhooks\"" not in (root / name).read_text() and
              "create_webhook" not in (root / name).read_text())


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        print(f"\n{fn.__name__}")
        fn()
    print(f"\nAll {len(tests)} Quo-webhook checks passed.")


if __name__ == "__main__":
    main()
