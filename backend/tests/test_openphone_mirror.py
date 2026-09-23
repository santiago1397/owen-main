"""THE test: the OpenPhone mirror reads, and can never write.

`app/integrations/openphone/` mirrors a phone system the company is migrating away from
onto the CRM's customer timelines. OpenPhone is a LIVE account on a real business number,
and the owner's rule is absolute:

    "i should not be able to text, call or answer from the crm using the quo phone
     number, just log everything live"

In OpenPhone a stray `POST /messages` does not fail a test — it TEXTS A REAL CUSTOMER and
bills for it. So the first three tests below do not check a flag or a status code; they
drive a full 30-day backfill against a recording transport and assert that **not one
non-GET request reached api.openphone.com**, and that the client module has no way to make
one.

The rest asserts the behaviour the owner asked for, the same way: by driving the real
`sync.run_once` against fakes and looking at what came out, never at a return value alone.

Stdlib only apart from the app itself, like every other test here.
Run: python -m tests.test_openphone_mirror
"""

import asyncio
import json
import re

OUR_LINE = "+19417247244"
OUR_LINE_ID = "PNRxH5G3uI"
CUSTOMER = "+19415550123"
STRANGER = "(941) 555-0199"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"openphone_mirror failed at: {name}")


# --- a transport that records every request and refuses to be surprised -------------------

class RecordedResponse:
    def __init__(self, payload, status=200, content=b"AUDIO"):
        self._payload = payload
        self.status_code = status
        self.content = content
        self.headers = {"content-type": "audio/mpeg"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class RecordingTransport:
    """Stands in for `httpx.AsyncClient`, recording (method, url, params) for everything.

    Every verb is implemented, INCLUDING the ones the mirror must never use — `post`, `put`,
    `patch` and `delete` are here on purpose. If they were absent, a stray write would fail
    with AttributeError and the test would pass for the wrong reason; present, it is
    recorded and the assertion below catches it as what it is.
    """

    def __init__(self, routes, fail_with=None):
        self.routes = routes
        self.fail_with = fail_with
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _record(self, method, url, **kw):
        self.calls.append({"method": method, "url": url,
                           "params": dict(kw.get("params") or {})})

    async def get(self, url, params=None, headers=None, **kw):
        self._record("GET", url, params=params)
        if self.fail_with is not None:
            raise self.fail_with
        for pattern, payload in self.routes.items():
            if re.search(pattern, url):
                return RecordedResponse(payload)
        return RecordedResponse({"data": []})

    async def post(self, url, **kw):
        self._record("POST", url, **kw)
        return RecordedResponse({})

    async def put(self, url, **kw):
        self._record("PUT", url, **kw)
        return RecordedResponse({})

    async def patch(self, url, **kw):
        self._record("PATCH", url, **kw)
        return RecordedResponse({})

    async def delete(self, url, **kw):
        self._record("DELETE", url, **kw)
        return RecordedResponse({})


# --- a session that behaves enough like the real one --------------------------------------

class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    """Tracks the `openphone_mirror_rows` this run added, so `_already_mirrored` gives the
    honest answer on a second pass — which is what makes the idempotency test mean
    something rather than testing a stub against itself."""

    def __init__(self, callers=(), mirrored=(), backfill_done=False):
        self.added = []
        self.mirrored = set(mirrored)
        self.callers = list(callers)
        self.backfill_done = backfill_done
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, stmt):
        text = str(stmt)
        params = {}
        try:
            params = stmt.compile().params
        except Exception:                      # noqa: BLE001 - not every stmt compiles bare
            pass
        if "openphone_mirror_rows" in text:
            key = (params.get("kind_1"), params.get("external_id_1"))
            return FakeResult([("id",)] if key in self.mirrored else [])
        if "callers" in text:
            return FakeResult(self.callers)
        return FakeResult([])

    async def get(self, model, key):
        if getattr(model, "__name__", "") == "AppSetting" and self.backfill_done:
            class Row:
                value = {"completed_at": "2026-09-01T00:00:00+00:00"}
            return Row()
        return None

    async def commit(self):
        self.commits += 1
        # The real `queue.enqueue` commits, which is when a mirror row becomes visible to
        # `_already_mirrored`. Mirroring that here is what lets one run see its own writes.
        for obj in self.added:
            kind = getattr(obj, "kind", None)
            if kind:
                self.mirrored.add((kind, obj.external_id))

    async def rollback(self):
        self.rollbacks += 1
        self.added = [o for o in self.added if not hasattr(o, "external_id")]


# --- the account, as D11a measured it -----------------------------------------------------

def account(calls=None, messages=None, conversations=None, contacts=None):
    return {
        r"/phone-numbers": {"data": [{"id": OUR_LINE_ID, "number": OUR_LINE,
                                      "name": "Business development"}]},
        r"/conversations": {"data": conversations if conversations is not None else [
            {"id": "CN1", "participants": [OUR_LINE, CUSTOMER],
             "lastActivityAt": "2026-09-11T10:00:00Z"}]},
        r"/contacts": {"data": contacts or []},
        r"/calls\b": {"data": calls if calls is not None else []},
        r"/messages": {"data": messages if messages is not None else []},
        r"/call-transcripts/": {"dialogue": [
            {"identifier": CUSTOMER, "content": "The skylight is leaking."}]},
        r"/call-summaries/": {"summary": "Customer reports a leaking skylight."},
        # The shape MEASURED on production 2026-09-14: `data` is a LIST of recordings.
        r"/call-recordings/": {"data": [{"duration": 184, "id": "REC1",
                                         "startTime": "2026-09-11T10:00:05Z",
                                         "status": "completed", "type": "audio/mpeg",
                                         "url": "https://share.quo.com/rec/abc.mp3"}]},
    }


A_CALL = {"id": "AC_call_1", "direction": "incoming", "status": "completed",
          "duration": 184, "createdAt": "2026-09-11T10:00:00Z",
          "participants": [OUR_LINE, CUSTOMER]}

A_TEXT = {"id": "AC_msg_1", "direction": "incoming", "text": "Can you come Tuesday?",
          "createdAt": "2026-09-11T10:05:00Z", "from": CUSTOMER, "to": OUR_LINE}


def run_mirror(routes, *, enabled=True, fail_with=None, session=None, dry_run=False,
               key="op_test_key"):
    """Drive the REAL `sync.run_once` against a recording transport.

    Returns `(result, transport, session)` so every assertion below is made against what
    actually went out on the wire, not against a mock's call log.
    """
    import httpx

    from app.core.config import settings
    from app.db import SessionLocal as _real
    from app.integrations.openphone import push as op_push
    from app.integrations.openphone import sync as op_sync
    from app.providers import openphone_client as op_client
    from app.services import queue as queue_mod

    transport = RecordingTransport(routes, fail_with=fail_with)
    session = session or FakeSession()
    jobs = []

    async def fake_enqueue(db, job_type, payload, delay_seconds=0):
        jobs.append((job_type, payload))
        await db.commit()

    saved = (settings.OPENPHONE_MIRROR_ENABLED, settings.OPENPHONE_API_KEY,
             settings.AGENT_RUNTIME_KEY, httpx.AsyncClient, op_sync.SessionLocal,
             queue_mod.enqueue, op_push.queue.enqueue)
    try:
        settings.OPENPHONE_MIRROR_ENABLED = enabled
        settings.OPENPHONE_API_KEY = key
        settings.AGENT_RUNTIME_KEY = "owen_sk_test"
        httpx.AsyncClient = lambda *a, **kw: transport
        op_client.httpx.AsyncClient = lambda *a, **kw: transport
        op_sync.SessionLocal = lambda: session
        queue_mod.enqueue = fake_enqueue
        op_push.queue.enqueue = fake_enqueue
        result = asyncio.run(op_sync.run_once(dry_run=dry_run))
    finally:
        (settings.OPENPHONE_MIRROR_ENABLED, settings.OPENPHONE_API_KEY,
         settings.AGENT_RUNTIME_KEY, httpx.AsyncClient, op_sync.SessionLocal,
         queue_mod.enqueue, op_push.queue.enqueue) = saved
        op_client.httpx.AsyncClient = httpx.AsyncClient
        assert _real is not None
    result["_jobs"] = jobs
    return result, transport, session


# ══════════════════════════════════════════════════════════════════════════════════════════
#  1. THE RULE. Read-only, proven three different ways.
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_a_full_backfill_issues_no_non_get_to_openphone():
    """The most important test in this module.

    Drives the whole thing — number discovery, conversation enumeration, the address book,
    calls, texts, transcripts, summaries, recordings — and asserts that every single request
    that reached api.openphone.com was a GET.
    """
    print("a full backfill, every request inspected:")
    result, transport, _ = run_mirror(account(calls=[A_CALL], messages=[A_TEXT]))

    openphone = [c for c in transport.calls if "api.openphone.com" in c["url"]]
    check("the backfill actually made requests (else this proves nothing)",
          len(openphone) > 0)
    print(f"       {len(openphone)} request(s) to api.openphone.com")
    non_get = [c for c in openphone if c["method"] != "GET"]
    for bad in non_get:
        print(f"       !! {bad['method']} {bad['url']}")
    check("ZERO non-GET requests to api.openphone.com", non_get == [])
    check("every OpenPhone path is a read path",
          all(not re.search(r"/send|/dial|/create", c["url"]) for c in openphone))
    check("the mirror did real work (a call and a text)",
          result["calls"] == 1 and result["messages"] == 1)


def test_the_client_module_has_no_way_to_write():
    """Structural, not behavioural: the guarantee is that there is no `_post` to misuse.

    A behavioural test can only prove the paths it walked. This one reads the source and
    proves there is no writer in the file at all — which is the claim the module's own
    header makes and the reason a reviewer can trust it without reading every caller.
    """
    print("openphone_client.py, read as source:")
    import ast
    import pathlib

    import app.providers.openphone_client as mod

    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    check("no _post / _put / _delete / _patch helper exists",
          not {"_post", "_put", "_delete", "_patch"} & names)

    # Any `<something>.post(...)` / .put / .patch / .delete anywhere in the module.
    writes = [n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr in {"post", "put", "patch", "delete", "request"}]
    check(f"no write verb is called anywhere in the file (found {writes})", writes == [])
    check("`_get` is still the only transport (client.get is the sole http call)",
          src.count("client.get(") >= 1)


def test_the_guard_refuses_an_action_shaped_path():
    """`_get` refuses a write-shaped path before the request leaves the process — so even a
    future edit that routes a send THROUGH the read helper is stopped."""
    print("the path guard:")
    from app.core.config import settings
    from app.providers import openphone_client as op_client

    saved = settings.OPENPHONE_API_KEY
    settings.OPENPHONE_API_KEY = "op_test_key"
    try:
        for path in ("/messages/send", "/calls/dial", "/contacts/create"):
            try:
                asyncio.run(op_client._get(path))
                check(f"{path} was refused", False)
            except RuntimeError as exc:
                check(f"{path} refused: {str(exc)[:40]}...", "read-only" in str(exc))
    finally:
        settings.OPENPHONE_API_KEY = saved


def test_nothing_in_the_module_mentions_an_openphone_send_path():
    """No send path, not even a disabled one — a disabled send path is a switch somebody
    eventually flips. Asserted over the whole package."""
    print("the module, read as source:")
    import pathlib

    import app.integrations.openphone as pkg

    root = pathlib.Path(pkg.__file__).parent
    offenders = []
    for path in sorted(root.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"'):
                continue          # prose about not sending is the point, not a violation
            if re.search(r"\.(post|put|patch|delete)\s*\(", stripped) and "op." in stripped:
                offenders.append(f"{path.name}: {stripped[:60]}")
    check(f"no write verb against the OpenPhone client ({offenders})", offenders == [])


# ══════════════════════════════════════════════════════════════════════════════════════════
#  2. The kill switch
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_with_the_kill_switch_off_nothing_runs_and_no_request_is_made():
    print("OPENPHONE_MIRROR_ENABLED=false:")
    result, transport, session = run_mirror(account(calls=[A_CALL]), enabled=False)
    check("the tick declined", result["ran"] is False)
    check("it said why", "OPENPHONE_MIRROR_ENABLED" in result["reason"])
    check("ZERO requests were made, to anything", transport.calls == [])
    check("no database row was written", session.added == [])
    check("no job was queued", result["_jobs"] == [])


def test_with_no_api_key_nothing_runs():
    """The second switch. An operator who enables the mirror where no key is configured gets
    a logged refusal, not a stream of 401s against a live account."""
    print("OPENPHONE_API_KEY empty:")
    result, transport, _ = run_mirror(account(), enabled=True, key="")
    check("the tick declined", result["ran"] is False)
    check("it named the key", "OPENPHONE_API_KEY" in result["reason"])
    check("ZERO requests were made", transport.calls == [])


# ══════════════════════════════════════════════════════════════════════════════════════════
#  3. What lands on the thread
# ══════════════════════════════════════════════════════════════════════════════════════════

def _crm_body(payload, contact_id=None):
    from app.integrations.openphone.events import to_crm_event
    return to_crm_event(payload, contact_id)


def test_a_mirrored_call_is_one_event_labelled_with_its_source():
    print("one OpenPhone call:")
    result, _, session = run_mirror(account(calls=[A_CALL]))
    jobs = [p for t, p in result["_jobs"] if t == "crm_report"]
    check("exactly ONE job was queued", len(jobs) == 1)

    payload = jobs[0]["body"]
    body = _crm_body(payload)
    print("       " + json.dumps({k: v for k, v in body.items()
                                  if k != "transcript"}, indent=None)[:300])
    check("it is a CALL", body["type"] == "CALL")
    check("INBOUND, as OpenPhone said", body["direction"] == "INBOUND")
    check("the duration survived", body["duration_seconds"] == 184)
    check("the status maps onto one the CRM accepts", body["call_status"] == "completed")
    check("it is labelled with the SYSTEM", body["source_system"] == "OpenPhone")
    check("it is labelled with the LINE it came through",
          body["source_number"] == OUR_LINE)
    check("the customer's number is sent so the CRM can match or create",
          body["from_number"] == CUSTOMER)
    check("it carries the real time it happened, not the ingest clock",
          body["occurred_at"].startswith("2026-09-11T10:00:00"))
    check("the sentence names the line too, so it survives a copy-paste",
          OUR_LINE in body["body"])
    check("the transcript rode along at no STT cost",
          "skylight" in (body.get("transcript") or ""))
    check("the recording points at the CRM's own route, not at OpenPhone",
          body["recording_url"] == "/api/openphone/recordings/AC_call_1"
          and "openphone.com" not in body["recording_url"])


def test_a_text_mirrors_with_its_direction_and_body():
    print("one OpenPhone text:")
    result, _, _ = run_mirror(account(messages=[A_TEXT]))
    jobs = [p for t, p in result["_jobs"] if t == "crm_report"]
    check("exactly ONE job was queued", len(jobs) == 1)
    body = _crm_body(jobs[0]["body"])
    check("it is an SMS", body["type"] == "SMS")
    check("INBOUND", body["direction"] == "INBOUND")
    check("the customer's words, verbatim and unprefixed",
          body["body"] == "Can you come Tuesday?")
    check("labelled with its source line", body["source_number"] == OUR_LINE)
    check("no call_status on a non-CALL (the CRM 400s on that)",
          "call_status" not in body)


def test_an_outbound_text_keeps_its_direction():
    print("an OUTBOUND OpenPhone text:")
    out = dict(A_TEXT, id="AC_msg_2", direction="outgoing",
               **{"from": OUR_LINE, "to": CUSTOMER})
    result, _, _ = run_mirror(account(messages=[out]))
    jobs = [p for t, p in result["_jobs"] if t == "crm_report"]
    body = _crm_body(jobs[0]["body"])
    check("OUTBOUND", body["direction"] == "OUTBOUND")
    check("the CUSTOMER is still who the thread belongs to",
          body["from_number"] == CUSTOMER)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  4. Idempotency — the same object twice is ONE event
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_the_same_call_ingested_twice_produces_one_event():
    """The poll re-reads the same window every tick, so this is the normal case, not an
    edge case. Both halves are asserted: the state row stops the second ENQUEUE, and the
    dedupe key it carries is what stops a second ROW if a retry gets past it anyway."""
    print("the same call, mirrored twice:")
    session = FakeSession()
    first, _, _ = run_mirror(account(calls=[A_CALL]), session=session)
    second, _, _ = run_mirror(account(calls=[A_CALL]), session=session)

    check("the first tick queued it", first["calls"] == 1)
    check("the second tick queued NOTHING", second["calls"] == 0)
    check("...and said it was already mirrored", second["duplicates"] == 1)
    total = len([p for t, p in first["_jobs"] + second["_jobs"] if t == "crm_report"])
    check(f"ONE job across both ticks, not two (got {total})", total == 1)


def test_the_dedupe_key_is_stable_and_namespaced():
    """The CRM stores this on a UNIQUE column. If it were not stable across processes and
    restarts, a re-backfill would duplicate a customer's whole history."""
    print("the dedupe key:")
    from app.integrations.openphone import config as op_config

    key = op_config.dedupe_key("call", "AC_call_1")
    check(f"namespaced and derived only from OpenPhone's id ({key})",
          key == "openphone:call:AC_call_1")
    check("stable across calls", key == op_config.dedupe_key("call", "AC_call_1"))
    check("a call and a message with the same id do NOT collide",
          key != op_config.dedupe_key("message", "AC_call_1"))


def test_a_dry_run_writes_nothing_at_all():
    print("preview (dry run):")
    result, transport, session = run_mirror(account(calls=[A_CALL], messages=[A_TEXT]),
                                            dry_run=True)
    check("it read OpenPhone", any("api.openphone.com" in c["url"]
                                   for c in transport.calls))
    check("it counted what it would send", result["calls"] + result["messages"] == 2)
    check("no state row was written", session.added == [])
    check("no job was queued", result["_jobs"] == [])


# ══════════════════════════════════════════════════════════════════════════════════════════
#  5. Identity — the last ten digits, the rule both systems share
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_the_match_key_is_the_last_ten_digits_and_agrees_with_the_crm_link():
    """Two systems that disagree about whether two renderings are the same line would file
    a customer's call on the wrong timeline. This pins the mirror's copy of the rule to the
    CRM link's, case by case."""
    print("last-ten-digits, both kernels:")
    from app.integrations.crm import config as crm_config
    from app.integrations.openphone import config as op_config

    for raw in ("+19415550123", "9415550123", "(941) 555-0123", "941-555-0123",
                "1 941 555 0123", "", "555-0123"):
        ours, theirs = op_config.match_key(raw), crm_config.match_key(raw)
        check(f"{raw!r:20} -> {ours!r:12} (crm link agrees)", ours == theirs)
    check("the four renderings of one number are one key",
          len({op_config.match_key(x) for x in
               ("+19415550123", "9415550123", "(941) 555-0123", "941-555-0123")}) == 1)


def test_an_unknown_number_is_sent_for_the_crm_to_create():
    """An unknown caller auto-creates a contact, exactly as the BulkVS path does. OWEN does
    not create it — it sends `from_number` and the CRM matches on the last ten digits or
    creates. Asserted here as 'the body carries what the CRM needs to do that'."""
    print("a stranger:")
    stranger_call = dict(A_CALL, id="AC_call_x", participants=[OUR_LINE, STRANGER])
    result, _, _ = run_mirror(account(
        calls=[stranger_call],
        conversations=[{"id": "CN9", "participants": [OUR_LINE, STRANGER],
                        "lastActivityAt": "2026-09-11T10:00:00Z"}]))
    jobs = [p for t, p in result["_jobs"] if t == "crm_report"]
    check("the stranger's call was mirrored, not dropped", len(jobs) == 1)
    body = _crm_body(jobs[0]["body"])
    check("no contact_id is claimed", "contact_id" not in body)
    check("the number is sent so the CRM can match-or-create",
          body["from_number"] == STRANGER)
    from app.integrations.openphone import config as op_config
    check("and it keys to the same ten digits however it was written",
          op_config.match_key(body["from_number"]) == op_config.match_key("9415550199"))


# ══════════════════════════════════════════════════════════════════════════════════════════
#  6. Degrading quietly — an outage must not break anything
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_a_401_stops_the_mirror_and_breaks_nothing_else():
    print("OpenPhone answers 401:")
    result, _, session = run_mirror(account(), fail_with=RuntimeError("HTTP 401"))
    check("run_once did NOT raise", isinstance(result, dict))
    check("it reported that it did not run", result["ran"] is False)
    check("it said why, without inventing a cause", "OpenPhone unreachable" in
          result["reason"])
    check("nothing was written", session.added == [])
    check("nothing was queued", result["_jobs"] == [])


def test_the_scheduled_poll_never_raises():
    """`poll()` runs inside the worker that also drains the queue for a LIVE phone system.
    A mirror of a system the company is leaving must never be able to disturb it."""
    print("poll() with OpenPhone on fire:")
    import httpx

    from app.core.config import settings
    from app.integrations.openphone import sync as op_sync

    def explode(*a, **kw):
        raise RuntimeError("the network is gone")

    saved = (settings.OPENPHONE_MIRROR_ENABLED, settings.OPENPHONE_API_KEY,
             httpx.AsyncClient)
    try:
        settings.OPENPHONE_MIRROR_ENABLED = True
        settings.OPENPHONE_API_KEY = "op_test_key"
        httpx.AsyncClient = explode
        asyncio.run(op_sync.poll())          # must simply return
        check("poll() returned instead of raising", True)
    except Exception as exc:                  # noqa: BLE001
        check(f"poll() raised {type(exc).__name__}", False)
    finally:
        (settings.OPENPHONE_MIRROR_ENABLED, settings.OPENPHONE_API_KEY,
         httpx.AsyncClient) = saved


def test_a_missing_conversations_endpoint_degrades_instead_of_failing():
    """`/conversations` is the one read the probe has NOT confirmed. If it is absent the
    mirror must narrow and SAY SO — reporting a partial pass as a clean one is the failure
    this flag exists to prevent."""
    print("/conversations unavailable:")
    routes = account(calls=[A_CALL], contacts=[
        {"defaultFields": {"phoneNumbers": [{"value": CUSTOMER}]}}])
    del routes[r"/conversations"]
    routes[r"/conversations"] = None          # placeholder; replaced below

    class Missing(RecordingTransport):
        async def get(self, url, params=None, headers=None, **kw):
            self._record("GET", url, params=params)
            if "/conversations" in url:
                raise RuntimeError("HTTP 404")
            return await RecordingTransport.get(self, url, params=params)

    import httpx

    from app.core.config import settings
    from app.integrations.openphone import push as op_push
    from app.integrations.openphone import sync as op_sync
    from app.services import queue as queue_mod

    routes.pop(r"/conversations", None)
    transport = Missing(routes)
    session = FakeSession()
    jobs = []

    async def fake_enqueue(db, job_type, payload, delay_seconds=0):
        jobs.append((job_type, payload))
        await db.commit()

    saved = (settings.OPENPHONE_MIRROR_ENABLED, settings.OPENPHONE_API_KEY,
             settings.AGENT_RUNTIME_KEY, httpx.AsyncClient, op_sync.SessionLocal,
             queue_mod.enqueue, op_push.queue.enqueue)
    try:
        settings.OPENPHONE_MIRROR_ENABLED = True
        settings.OPENPHONE_API_KEY = "op_test_key"
        settings.AGENT_RUNTIME_KEY = "owen_sk_test"
        httpx.AsyncClient = lambda *a, **kw: transport
        op_sync.SessionLocal = lambda: session
        queue_mod.enqueue = fake_enqueue
        op_push.queue.enqueue = fake_enqueue
        result = asyncio.run(op_sync.run_once())
    finally:
        (settings.OPENPHONE_MIRROR_ENABLED, settings.OPENPHONE_API_KEY,
         settings.AGENT_RUNTIME_KEY, httpx.AsyncClient, op_sync.SessionLocal,
         queue_mod.enqueue, op_push.queue.enqueue) = saved

    check("the tick still ran", result["ran"] is True)
    check("it fell back to the address book and still mirrored the call",
          result["calls"] == 1)
    check("it reported the participant set as INCOMPLETE rather than clean",
          result["complete"] is False)
    check("and did NOT close the backfill on a partial pass",
          not result.get("backfill_closed"))


# ══════════════════════════════════════════════════════════════════════════════════════════
#  7. The surface, fenced
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_the_router_is_exactly_these_routes_and_the_crm_link_is_untouched():
    """A fence, in the spirit of `test_crm_softphone_creds`'s: the next edit that moves or
    re-gates a route fails HERE first, before a deploy."""
    print("the public surface:")
    from app.core.apikeys import SCOPE_AGENT_WRITE, SCOPE_CRM_LINK
    from app.integrations.openphone.api import router

    routes = sorted({r.path for r in router.routes})
    expected = sorted({
        "/api/openphone-mirror/events",
        "/api/openphone-mirror/recordings/{call_id}",
        "/api/openphone-mirror/status",
        "/api/openphone-mirror/preview",
        # 2026-09-13: where a verified Quo webhook event is processed. Worker-only
        # (agent_write), like /events. The public receiver is NOT on this router.
        "/api/openphone-mirror/webhook-events",
    })
    check(f"exactly five routes: {routes}", routes == expected)
    check("no route mentions sending, dialling or answering",
          not any(re.search(r"send|dial|answer|call$", p) for p in routes))

    import app.main as main_mod

    crm = sorted({r.path for r in main_mod.app.routes
                  if getattr(r, "path", "").startswith("/api/crm-link")})
    # Fourteen since 2026-09-23: /live-calls, /live-calls/{linkedid}/listen and /takeover
    # let the CRM show a live AI-agent call and ring its user in to hear or seize it
    # (voice agents phase 1, slice E). None of them is the mirror's.
    # Eleven since 2026-09-22: /recordings/{call_id} serves an AI-agent call's audio to the
    # CRM, which proxies it onto the thread (phase 1 of the voice-agent amendment).
    # Ten since 2026-09-16: /email-jobs (the AHS email hop, feature/ahs-email-to-crm) and
    # then /media + /messages/{id}/media/{i} (pictures on a CRM text,
    # feature/mms-media-relay) were added to the CRM link on purpose. The mirror still
    # adds none, which is what this line is actually for.
    check(f"the CRM link still has its fourteen routes, unwidened by the mirror ({len(crm)})",
          len(crm) == 14 and "/api/crm-link/email-jobs" in crm)
    check("scopes exist and are the two reused ones",
          bool(SCOPE_AGENT_WRITE) and bool(SCOPE_CRM_LINK))


def test_the_recording_path_matches_what_the_crm_serves():
    """Two repositories agreeing on a string is exactly what rots silently. Both ends pin
    it; the CRM's `test_openphone_thread.py` asserts its route is mounted here."""
    print("the recording path contract:")
    from app.integrations.openphone.events import CRM_RECORDING_PATH

    check(f"owen-main builds {CRM_RECORDING_PATH}/<id>",
          CRM_RECORDING_PATH == "/api/openphone/recordings")
    check("it is a CRM-relative path, so the browser sends its CRM cookie and no key",
          CRM_RECORDING_PATH.startswith("/") and "://" not in CRM_RECORDING_PATH)


def test_the_line_selection_is_configurable_without_a_code_change():
    print("which lines are mirrored:")
    from app.integrations.openphone import config as op_config

    class S:
        OPENPHONE_MIRROR_ENABLED = True
        OPENPHONE_API_KEY = "k"
        OPENPHONE_MIRROR_NUMBERS = ""
        OPENPHONE_MIRROR_EXCLUDE_NUMBERS = ""

    view = op_config.settings_view(S)
    check("empty include = every line on the account", view.mirrors(OUR_LINE))

    S.OPENPHONE_MIRROR_EXCLUDE_NUMBERS = "941-724-7244"
    view = op_config.settings_view(S)
    check("an excluded line is excluded, however it is written",
          not view.mirrors(OUR_LINE))

    S.OPENPHONE_MIRROR_EXCLUDE_NUMBERS = ""
    S.OPENPHONE_MIRROR_NUMBERS = "+19995550000"
    view = op_config.settings_view(S)
    check("an include list that omits our line excludes it",
          not view.mirrors(OUR_LINE))
    check("and includes the one it names", view.mirrors("+1 999 555 0000"))


def test_an_excluded_line_is_never_read():
    """Not just filtered out of the results — never asked about at all."""
    print("an excluded line, on the wire:")
    from app.core.config import settings

    saved = settings.OPENPHONE_MIRROR_EXCLUDE_NUMBERS
    try:
        settings.OPENPHONE_MIRROR_EXCLUDE_NUMBERS = OUR_LINE
        result, transport, _ = run_mirror(account(calls=[A_CALL]))
    finally:
        settings.OPENPHONE_MIRROR_EXCLUDE_NUMBERS = saved
    check("the tick declined", result["ran"] is False)
    check("only /phone-numbers was read — no call or message request went out",
          all("/calls" not in c["url"] and "/messages" not in c["url"]
              for c in transport.calls))


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        print(f"\n{fn.__name__}")
        fn()
    print(f"\nAll {len(tests)} OpenPhone-mirror checks passed.")


if __name__ == "__main__":
    main()
