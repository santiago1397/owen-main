"""The EXACT query strings the mirror sends to Quo, and what one bad request costs.

The first live preview on production (2026-09-14) came back `calls 0, messages 0, errors 2,
participants 200, complete false`, with `GET /messages ... participants%5B%5D=... -> 400`.
Quo's API reference says array parameters are a repeated key WITHOUT brackets. These tests
pin the bytes on the wire — built by httpx itself, exactly as `AsyncClient.get` builds
them — so a bracket can never creep back in, and they pin that one participant's failure
is counted, redacted and survivable.

No request leaves the process: every transport here is a fake that answers with
`httpx.Response` objects. Stdlib only apart from the app itself.
Run: python -m tests.test_openphone_query
"""

import asyncio
import contextlib
import io
import json
import re

import httpx

from tests.test_openphone_mirror import (CUSTOMER, OUR_LINE, OUR_LINE_ID, FakeSession,
                                         check)

BASE = "https://api.openphone.com/v1"
DIGITS = re.compile(r"\d{7,}")


class WireTransport:
    """Stands in for httpx.AsyncClient. Builds each request the way httpx does, records
    the full URL, and answers with a real `httpx.Response` so `raise_for_status` raises a
    real `HTTPStatusError` carrying the request and response."""

    def __init__(self, answer):
        self.answer = answer
        self.urls = []
        self.methods = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None, **kw):
        request = httpx.Request("GET", url, params=params)
        self.urls.append(str(request.url))
        self.methods.append("GET")
        status, payload = self.answer(request)
        return httpx.Response(status, json=payload, request=request)

    async def _write(self, method, url, **kw):  # present so a stray write is RECORDED
        self.urls.append(url)
        self.methods.append(method)
        return httpx.Response(200, json={}, request=httpx.Request(method, url))

    async def post(self, url, **kw):
        return await self._write("POST", url, **kw)

    async def put(self, url, **kw):
        return await self._write("PUT", url, **kw)

    async def patch(self, url, **kw):
        return await self._write("PATCH", url, **kw)

    async def delete(self, url, **kw):
        return await self._write("DELETE", url, **kw)


@contextlib.contextmanager
def wired(answer, *, mirror=True):
    from app.core.config import settings
    from app.integrations.openphone import push as op_push
    from app.providers import openphone_client as op_client
    from app.services import queue as queue_mod

    transport = WireTransport(answer)
    jobs = []

    async def fake_enqueue(db, job_type, payload, delay_seconds=0):
        jobs.append((job_type, payload))
        await db.commit()

    saved = (settings.OPENPHONE_MIRROR_ENABLED, settings.OPENPHONE_API_KEY,
             settings.AGENT_RUNTIME_KEY, op_client.httpx.AsyncClient,
             queue_mod.enqueue, op_push.queue.enqueue)
    try:
        settings.OPENPHONE_MIRROR_ENABLED = mirror
        settings.OPENPHONE_API_KEY = "op_test_key"
        settings.AGENT_RUNTIME_KEY = "owen_sk_test"
        op_client.httpx.AsyncClient = lambda *a, **kw: transport
        queue_mod.enqueue = fake_enqueue
        op_push.queue.enqueue = fake_enqueue
        yield transport, jobs
    finally:
        (settings.OPENPHONE_MIRROR_ENABLED, settings.OPENPHONE_API_KEY,
         settings.AGENT_RUNTIME_KEY, op_client.httpx.AsyncClient,
         queue_mod.enqueue, op_push.queue.enqueue) = saved


def ok(payload):
    return lambda request: (200, payload)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  1. The bytes on the wire
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_the_messages_query_string_is_exactly_what_quo_documents():
    print("GET /messages:")
    from app.providers import openphone_client as op

    with wired(ok({"data": []})) as (t, _):
        asyncio.run(op.list_messages(OUR_LINE_ID, "+19415550123", limit=50))
        asyncio.run(op.list_messages(OUR_LINE_ID, "+19415550123", limit=50, page_token="tok2"))
    check(f"first page: {t.urls[0]}", t.urls[0] ==
          BASE + "/messages?phoneNumberId=PNRxH5G3uI&participants=%2B19415550123&maxResults=50")
    check(f"next page: {t.urls[1]}", t.urls[1] ==
          BASE + "/messages?phoneNumberId=PNRxH5G3uI&participants=%2B19415550123"
                 "&maxResults=50&pageToken=tok2")


def test_the_calls_query_string_is_exactly_what_quo_documents():
    print("GET /calls:")
    from app.providers import openphone_client as op

    with wired(ok({"data": []})) as (t, _):
        asyncio.run(op.list_calls_with(OUR_LINE_ID, "+19415550123", limit=50))
    check(f"{t.urls[0]}", t.urls[0] ==
          BASE + "/calls?phoneNumberId=PNRxH5G3uI&participants=%2B19415550123&maxResults=50")


def test_the_conversations_query_string_uses_phone_numbers():
    print("GET /conversations:")
    from app.providers import openphone_client as op

    with wired(ok({"data": []})) as (t, _):
        asyncio.run(op.list_conversations(OUR_LINE_ID, limit=50))
    check(f"{t.urls[0]}", t.urls[0] == BASE + "/conversations?phoneNumbers=PNRxH5G3uI&maxResults=50")


def test_no_request_in_a_whole_run_carries_brackets_or_a_non_e164_participant():
    print("a full preview, every URL inspected:")
    from app.integrations.openphone import sync

    def answer(request):
        path = request.url.path
        if path.endswith("/phone-numbers"):
            return 200, {"data": [{"id": OUR_LINE_ID, "number": OUR_LINE}]}
        if path.endswith("/conversations"):
            return 404, {"message": "not here"}
        if path.endswith("/contacts"):
            # Typed the way a person types — the mirror must normalise before asking.
            return 200, {"data": [{"defaultFields": {"phoneNumbers": [
                {"value": "(941) 555-0123"}, {"value": "5550"}]}}]}
        return 200, {"data": []}

    session = FakeSession()
    saved = sync.SessionLocal
    try:
        sync.SessionLocal = lambda: session
        with wired(answer) as (t, _):
            result = asyncio.run(sync.run_once(dry_run=True, force_backfill=True))
    finally:
        sync.SessionLocal = saved
    lists = [u for u in t.urls if "/calls?" in u or "/messages?" in u]
    check(f"it asked about the participant ({len(lists)} list requests)", len(lists) == 2)
    check("no bracket anywhere", not any("%5B" in u or "[" in u for u in t.urls))
    check("the participant went as E.164",
          all("participants=%2B19415550123" in u for u in lists))
    src = result["participant_sources"][0]
    check(f"the short number was skipped, not sent ({src})", src["skipped_not_e164"] == 1)
    check("every request was a GET", set(t.methods) == {"GET"})


# ══════════════════════════════════════════════════════════════════════════════════════════
#  2. One failure is counted, redacted and survivable
# ══════════════════════════════════════════════════════════════════════════════════════════

TWO = [CUSTOMER, "+19415550188"]


def conversations_with(participants):
    return {"data": [{"id": "CN%d" % i, "participants": [OUR_LINE, p],
                      "lastActivityAt": "2099-01-01T00:00:00Z"}
                     for i, p in enumerate(participants)]}


def run_preview(answer):
    from app.integrations.openphone import sync

    session = FakeSession()
    saved = sync.SessionLocal
    try:
        sync.SessionLocal = lambda: session
        with wired(answer) as (t, jobs):
            result = asyncio.run(sync.run_once(dry_run=True, force_backfill=True))
    finally:
        sync.SessionLocal = saved
    return result, t


def test_one_participants_400_does_not_cost_the_others_their_mirror():
    print("participant A's /messages 400s, B's works:")

    def answer(request):
        path, q = request.url.path, str(request.url.query)
        if path.endswith("/phone-numbers"):
            return 200, {"data": [{"id": OUR_LINE_ID, "number": OUR_LINE}]}
        if path.endswith("/conversations"):
            return 200, conversations_with(TWO)
        if path.endswith("/messages") and "5550123" in q:
            return 400, {"message": "Bad participant +19415550123"}
        if path.endswith("/messages"):
            return 200, {"data": [{"id": "M1", "direction": "incoming", "text": "hi",
                                   "createdAt": "2099-01-01T00:00:00Z",
                                   "from": "+19415550188", "to": OUR_LINE}]}
        return 200, {"data": []}

    result, _ = run_preview(answer)
    check(f"B's text was still counted (messages={result['messages']})", result["messages"] == 1)
    errs = result["errors"]
    check(f"one error entry, counting ONE participant ({errs})",
          len(errs) == 1 and errs[0]["participants"] == 1 and errs[0]["status"] == 400)
    check("it names the resource, path and parameter names",
          errs[0]["resource"] == "messages" and "GET /v1/messages?" in errs[0]["error"]
          and "participants=…" in errs[0]["error"])
    check(f"and carries NO phone number ({errs[0]['error']})",
          not DIGITS.search(json.dumps(errs)))
    check("/conversations worked, so the address book was not used",
          result["participant_sources"][0]["conversations"] == "ok"
          and "from_address_book" not in result["participant_sources"][0])


def test_a_request_every_participant_gets_wrong_is_abandoned_after_five():
    print("every /messages 400s (the production shape), /calls works:")
    many = ["+1941555%04d" % i for i in range(20)]

    def answer(request):
        path = request.url.path
        if path.endswith("/phone-numbers"):
            return 200, {"data": [{"id": OUR_LINE_ID, "number": OUR_LINE}]}
        if path.endswith("/conversations"):
            return 200, conversations_with(many)
        if path.endswith("/messages"):
            return 400, {"message": "participants is invalid"}
        return 200, {"data": []}

    result, t = run_preview(answer)
    asked_messages = len([u for u in t.urls if "/messages?" in u])
    asked_calls = len([u for u in t.urls if "/calls?" in u])
    check(f"stopped asking /messages after 5 (asked {asked_messages})", asked_messages == 5)
    check(f"/calls still asked for all 20 (asked {asked_calls})", asked_calls == 20)
    by = {(e["resource"], e["status"]): e for e in result["errors"]}
    check("the 400 counts the five it hit", by[("messages", 400)]["participants"] == 5)
    check("the rest are reported as skipped, not silently dropped",
          by[("messages", None)]["participants"] == 15)
    check("no phone number in any error", not DIGITS.search(json.dumps(result["errors"])))


def test_a_failed_conversations_read_is_reported_with_its_reason_and_the_real_count():
    print("/conversations 400s -> fallback, counted:")
    from app.core.config import settings

    book = {"data": [{"defaultFields": {"phoneNumbers": [{"value": "+1941555%04d" % i}]}}
                     for i in range(7)]}

    def answer(request):
        path = request.url.path
        if path.endswith("/phone-numbers"):
            return 200, {"data": [{"id": OUR_LINE_ID, "number": OUR_LINE}]}
        if path.endswith("/conversations"):
            return 400, {"message": "unknown parameter"}
        if path.endswith("/contacts"):
            return 200, book
        return 200, {"data": []}

    saved = settings.OPENPHONE_MIRROR_MAX_PARTICIPANTS
    try:
        settings.OPENPHONE_MIRROR_MAX_PARTICIPANTS = 5
        result, _ = run_preview(answer)
    finally:
        settings.OPENPHONE_MIRROR_MAX_PARTICIPANTS = saved
    src = result["participant_sources"][0]
    check(f"reason recorded ({src['conversations']})",
          src["conversations"].startswith("failed: HTTPStatusError | 400"))
    check("fallback counts reported", src["from_address_book"] == 7)
    check("the REAL count before the ceiling", src["participants_found"] == 7)
    check("and that the ceiling truncated it", src["truncated_by_ceiling"] is True
          and result["participants"] == 5 and result["complete"] is False)


def test_preview_prints_the_errors_redacted():
    print("manage preview output:")
    from app.integrations.openphone import manage, sync

    fake = {"ran": True, "dry_run": True, "complete": False, "calls": 0, "messages": 0,
            "participant_sources": [{"conversations": "ok", "from_conversations": 3,
                                     "participants_found": 3, "skipped_not_e164": 0,
                                     "truncated_by_ceiling": False}],
            "errors": [{"resource": "messages", "status": 400, "participants": 3,
                        "error": "HTTPStatusError | 400 | GET /v1/messages?maxResults=…"
                                 "&participants=…&phoneNumberId=… | quo said: bad"}]}

    async def fake_run_once(**kw):
        return fake

    saved = sync.run_once
    out = io.StringIO()
    try:
        sync.run_once = fake_run_once
        with contextlib.redirect_stdout(out):
            asyncio.run(manage.cmd_preview(None))
    finally:
        sync.run_once = saved
    text = out.getvalue()
    check("an ERROR line per error, with the participant count",
          "ERROR messages: 3 participant(s) — HTTPStatusError | 400" in text)
    check("the participant sources are explained", "/conversations ok; 3 found" in text)


def test_the_redactor_removes_numbers_in_every_shape_quo_echoes():
    print("redaction:")
    from app.integrations.openphone import config as op_config

    raw = ("Client error '400 Bad Request' for url 'https://api.openphone.com/v1/messages?"
           "phoneNumberId=PNRxH5G3uI&participants%5B%5D=%2B19415550123&maxResults=50' "
           "+1 (941) 555-0123 9415550123")
    out = op_config.redact(raw)
    check(f"no seven-digit run survives ({out})", not DIGITS.search(out))
    check("the line id is kept (it is ours, not a customer's)", "PNRxH5G3uI" in out)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        print(f"\n{fn.__name__}")
        fn()
    print(f"\nAll {len(tests)} OpenPhone query checks passed.")


if __name__ == "__main__":
    main()
