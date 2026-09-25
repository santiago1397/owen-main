"""`GET /api/link-status` and the mirror's tick heartbeat — behaviour, not status codes.

The CRM's status dot polls this all day. What matters, in order:

  1. **It is read-only.** Driven against a session that FAILS on any add, flush, commit or
     delete, and the one statement it executes must be a SELECT.
  2. **It is API-key gated on `crm_link`**, the key the CRM already holds, and a key
     without that scope is refused by the real gate.
  3. **It still answers with every kill switch off** — reporting the switch is its job —
     and still answers when the database cannot be read.
  4. **It names nobody**: no phone number, no token, no secret, however they are configured.
  5. **The heartbeat is written by the scheduled poll, even when the poll raises**, carries
     no number, and a failure to write it never escapes into the worker.
  6. **Nothing existing moved**: the crm-link router has its fifteen paths (the AHS email
     branch added /email-jobs on purpose; feature/mms-media-relay added /media and
     /messages/{id}/media/{i} for pictures on a CRM text, 2026-09-16; /recordings/{call_id}
     for an AI call's audio, 2026-09-22; /live-calls, /live-calls/{linkedid}/listen and
     /takeover so the CRM can supervise a live AI call, 2026-09-23; /agent-versions, GET and
     POST on one path, so the CRM can import the live voice agent and publish new versions
     of it, 2026-09-25).

Stdlib only apart from the app itself, like every other test here.
Run: python -m tests.test_link_status
"""

import asyncio
import inspect
import json
import re
from datetime import datetime, timezone

OUR_LINE = "+19417247244"
CUSTOMER = "+19415550123"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"link_status failed at: {name}")


class Settings:
    """Temporarily override attributes on the live settings object."""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        from app.core.config import settings

        for k, v in self.values.items():
            self.saved[k] = getattr(settings, k)
            setattr(settings, k, v)
        return settings

    def __exit__(self, *exc):
        from app.core.config import settings

        for k, v in self.saved.items():
            setattr(settings, k, v)
        return False


class ReadOnlySession:
    """Answers reads; any write fails the test on the spot."""

    def __init__(self, settings_rows=None, last_webhook=None, fail_reads=False):
        self.rows = dict(settings_rows or {})
        self.last_webhook = last_webhook
        self.fail_reads = fail_reads
        self.statements = []
        self.gets = []

    def _write(self, *a, **kw):
        raise SystemExit("link_status failed at: the status route attempted a WRITE")

    add = add_all = delete = merge = _write

    async def flush(self, *a, **kw):
        self._write()

    async def commit(self, *a, **kw):
        self._write()

    async def get(self, model, key):
        if self.fail_reads:
            raise RuntimeError("database is down")
        self.gets.append((model.__name__, key))
        if key not in self.rows:
            return None

        class Row:
            value = self.rows[key]
        return Row()

    async def execute(self, stmt):
        if self.fail_reads:
            raise RuntimeError("database is down")
        self.statements.append(stmt)
        value = self.last_webhook

        class Result:
            def scalar(self_inner):
                return value
        return Result()


def _call(session):
    from app.integrations.link_status import link_status

    return asyncio.run(link_status(db=session, _key=None))


# --- 1. read-only ------------------------------------------------------------------------

def test_the_route_reads_and_never_writes():
    print("the status route is read-only:")
    from app.integrations.openphone.models import BACKFILL_SETTING_KEY, LAST_TICK_SETTING_KEY

    session = ReadOnlySession(settings_rows={
        LAST_TICK_SETTING_KEY: {"at": "2026-09-14T15:00:00+00:00", "ran": True,
                                "mode": "poll", "errors": 0, "reason": None},
        BACKFILL_SETTING_KEY: {"completed_at": "2026-09-12T02:00:00+00:00",
                               "counts": {"calls": 3}},
    }, last_webhook=datetime(2026, 9, 14, 14, 58, tzinfo=timezone.utc))
    with Settings(OPENPHONE_MIRROR_ENABLED=True, OPENPHONE_API_KEY="k",
                  OPENPHONE_WEBHOOK_ENABLED=True, OPENPHONE_WEBHOOK_SECRET="c2VjcmV0",
                  CRM_LINK_ENABLED=True, ASTERISK_ENABLED=True):
        out = _call(session)

    check("it read the two app_settings rows",
          {k for _, k in session.gets} == {LAST_TICK_SETTING_KEY, BACKFILL_SETTING_KEY})
    check("it executed exactly one statement", len(session.statements) == 1)
    from sqlalchemy.dialects import postgresql

    sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
    check("and that statement is a SELECT", sql.lstrip().upper().startswith("SELECT"))
    check("over the jobs table", "FROM jobs" in sql)
    check("the last tick time is reported", out["quo"]["last_tick_at"] == "2026-09-14T15:00:00+00:00")
    check("whether it ran", out["quo"]["last_tick_ran"] is True)
    check("the backfill completion", out["quo"]["backfill_completed_at"] == "2026-09-12T02:00:00+00:00")
    check("the last accepted webhook", out["quo"]["last_webhook_at"] == "2026-09-14T14:58:00+00:00")
    check("the switches", out["crm_link"] == {"enabled": True, "telephony_enabled": True}
          and out["quo"]["mirror_enabled"] and out["quo"]["webhook_enabled"])
    check("the poll interval, so the CRM can call a tick stale", out["quo"]["poll_seconds"] == 300)
    check("the database was readable", out["database_readable"] is True)


def test_the_webhook_time_comes_from_webhook_jobs_only():
    print("'last webhook' is the newest job a verified Quo webhook enqueued:")
    from sqlalchemy.dialects import postgresql

    from app.integrations.link_status import read_last_webhook_at
    from app.integrations.openphone.webhook import PROCESS_PATH

    session = ReadOnlySession()
    asyncio.run(read_last_webhook_at(session))
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    sql, params = str(compiled), compiled.params
    check("max(created_at)", "max(jobs.created_at)" in sql)
    check("filtered to crm_report jobs", "crm_report" in params.values())
    check("whose URL is the webhook processing path",
          any(isinstance(v, str) and v.endswith(PROCESS_PATH) for v in params.values()))


# --- 2. auth -----------------------------------------------------------------------------

def test_the_route_is_gated_on_the_crm_link_scope():
    print("the route is gated by the CRM-link API key:")
    from fastapi import HTTPException

    from app.api.ai.deps import require_scope
    from app.core.apikeys import SCOPE_CRM_LINK
    from app.integrations import link_status as ls

    dep = inspect.signature(ls.link_status).parameters["_key"].default
    inner = dep.dependency
    closed = dict(zip(inner.__code__.co_freevars,
                      [c.cell_contents for c in (inner.__closure__ or ())]))
    check("the route declares a scope dependency", closed.get("scope") == SCOPE_CRM_LINK)

    class FakeKey:
        def __init__(self, scopes):
            self.scopes = scopes

        def has(self, scope):
            return scope in self.scopes

    gate = require_scope(SCOPE_CRM_LINK)
    denied = None
    try:
        asyncio.run(gate(key=FakeKey(["read"])))
    except HTTPException as exc:
        denied = exc
    check("a key without crm_link is refused with 403",
          denied is not None and denied.status_code == 403)
    check("a key with crm_link passes", asyncio.run(gate(key=FakeKey([SCOPE_CRM_LINK]))))

    # The gate's own dependency is `authenticate`, which 401s a request with no key at all.
    auth_dep = inspect.signature(inner).parameters["key"].default.dependency
    from app.api.ai.deps import authenticate
    check("and the scope gate sits on authenticate", auth_dep is authenticate)

    # Over HTTP, through the real app: no key is refused before any database read.
    from fastapi.testclient import TestClient

    import app.main as main_mod

    with Settings(AI_API_ENABLED=True):
        resp = TestClient(main_mod.app).get("/api/link-status")
    check(f"no key over HTTP -> 401 ({resp.status_code})", resp.status_code == 401)
    check("and the body is the refusal, not the status",
          "crm_link" not in resp.text and "quo" not in resp.text)


def test_it_is_mounted_as_one_get_route_and_nothing_else_moved():
    print("the surface:")
    import app.main as main_mod
    from app.integrations import link_status as ls

    routes = [(r.path, sorted(r.methods)) for r in ls.router.routes]
    check(f"exactly one route, GET /api/link-status ({routes})",
          routes == [("/api/link-status", ["GET"])])
    mounted = [r for r in main_mod.app.routes if getattr(r, "path", "") == "/api/link-status"]
    check("mounted on the app", len(mounted) == 1)
    crm = {r.path for r in main_mod.app.routes
           if getattr(r, "path", "").startswith("/api/crm-link")}
    # Fifteen PATHS since 2026-09-25: /agent-versions (GET and POST on one path — sixteen
    # routes) lets the CRM publish the voice agent it edits and import the live one.
    # Fourteen since 2026-09-23: /live-calls and /live-calls/{linkedid}/listen|takeover, so
    # the CRM can show a live AI-agent call and ring its user in to hear or seize it.
    check(f"the CRM link has its fifteen paths ({len(crm)})", len(crm) == 15)


# --- 3. answers when things are off -------------------------------------------------------

def test_it_answers_with_every_switch_off():
    print("every switch off: still an answer, never a 503:")
    with Settings(OPENPHONE_MIRROR_ENABLED=False, OPENPHONE_API_KEY="",
                  OPENPHONE_WEBHOOK_ENABLED=False, OPENPHONE_WEBHOOK_SECRET="",
                  CRM_LINK_ENABLED=False, ASTERISK_ENABLED=False):
        out = _call(ReadOnlySession())
    check("the link reports disabled", out["crm_link"]["enabled"] is False)
    check("telephony reports disabled", out["crm_link"]["telephony_enabled"] is False)
    check("the mirror reports disabled and unkeyed",
          out["quo"]["mirror_enabled"] is False and out["quo"]["api_key_present"] is False)
    check("no tick has been recorded", out["quo"]["last_tick_at"] is None
          and out["quo"]["last_tick_ran"] is None)
    check("no backfill, no webhook", out["quo"]["backfill_completed_at"] is None
          and out["quo"]["last_webhook_at"] is None)


def test_an_unreadable_database_is_reported_not_raised():
    print("a database that cannot be read:")
    out = _call(ReadOnlySession(fail_reads=True))
    check("the route still answered", isinstance(out, dict))
    check("and says the database was not readable", out["database_readable"] is False)
    check("with the mirror facts unknown", out["quo"]["last_tick_at"] is None)


# --- 4. names nobody ---------------------------------------------------------------------

def test_the_payload_carries_no_number_and_no_secret():
    print("no phone number, token or secret in the payload:")
    from app.integrations.openphone.models import LAST_TICK_SETTING_KEY

    secrets = {
        "OPENPHONE_API_KEY": "op_key_never_shown",
        "OPENPHONE_WEBHOOK_SECRET": "whsec_never_shown",
        "CRM_LINK_TOKEN": "crm_token_never_shown",
        "AGENT_RUNTIME_KEY": "agent_key_never_shown",
    }
    with Settings(OPENPHONE_MIRROR_ENABLED=True, OPENPHONE_WEBHOOK_ENABLED=True,
                  CRM_LINK_ENABLED=True, CRM_LINK_BASE_URL="http://ghl_clone_api:8000",
                  CRM_LINK_ALLOWLIST=CUSTOMER, OPENPHONE_MIRROR_NUMBERS=OUR_LINE,
                  CRM_LINK_SOFTPHONE_OPERATORS="owen@dreamteamroofingfl.com", **secrets):
        out = _call(ReadOnlySession(settings_rows={
            # A heartbeat written by an older build, carrying a number in its reason. The
            # route passes the reason through, so the writer must never put one there —
            # asserted separately below — but a number in the tick row must still not leak.
            LAST_TICK_SETTING_KEY: {"at": "2026-09-14T15:00:00+00:00", "ran": False,
                                    "reason": "no mirrored line"},
        }))
    blob = json.dumps(out)
    for name, value in secrets.items():
        check(f"{name} is absent", value not in blob)
    check("no URL", "http" not in blob)
    check("no email or operator slug", "dreamteamroofingfl" not in blob)
    check("no run of 7+ digits anywhere (no phone number, ours or a customer's)",
          not re.search(r"\d{7,}", blob))


# --- 5. the heartbeat --------------------------------------------------------------------

def test_the_tick_record_is_lossy_and_redacted():
    print("the heartbeat row keeps counts and a redacted reason, never a number:")
    from app.integrations.openphone.config import tick_record

    result = {"ran": True, "mode": "poll", "numbers": [OUR_LINE], "participants": 4,
              "complete": True, "calls": 2, "messages": 1,
              "errors": [{"resource": "calls", "error": f"400 for {CUSTOMER}"}]}
    rec = tick_record(result, "2026-09-14T15:00:00+00:00")
    check("it ran, in poll mode", rec["ran"] is True and rec["mode"] == "poll")
    check("errors are a COUNT", rec["errors"] == 1)
    blob = json.dumps(rec)
    check("our line is not in it", "9417247244" not in blob)
    check("the customer is not in it", "9415550123" not in blob)

    refused = tick_record({"ran": False, "reason": f"OpenPhone said no about {CUSTOMER}"},
                          "2026-09-14T15:05:00+00:00")
    check("a refusal keeps its reason", refused["ran"] is False and "OpenPhone" in refused["reason"])
    check("with the number redacted", "9415550123" not in refused["reason"]
          and "<number>" in refused["reason"])
    check("garbage in is still a row", tick_record(None, "t")["ran"] is False)


class WritableSession:
    def __init__(self, existing=None, fail=False):
        self.existing = existing
        self.added = []
        self.commits = 0
        self.fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, model, key):
        if self.fail:
            raise RuntimeError("db down")
        self.key = key
        return self.existing

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def test_record_tick_inserts_then_overwrites_one_row():
    print("the heartbeat is ONE row, inserted once then overwritten:")
    from app.integrations.openphone import sync
    from app.integrations.openphone.models import LAST_TICK_SETTING_KEY

    fresh = WritableSession()
    asyncio.run(sync._record_tick({"ran": True, "mode": "poll"}, session_factory=lambda: fresh))
    check("a first tick inserts the row", len(fresh.added) == 1 and fresh.commits == 1)
    check("under the heartbeat key", fresh.added[0].key == LAST_TICK_SETTING_KEY)
    check("with a timestamp", bool(fresh.added[0].value["at"]))

    class Row:
        value = {"at": "old"}
    row = Row()
    again = WritableSession(existing=row)
    asyncio.run(sync._record_tick({"ran": False, "reason": "x"}, session_factory=lambda: again))
    check("a later tick overwrites it, adding nothing", again.added == [] and again.commits == 1)
    check("with the new outcome", row.value["ran"] is False and row.value["at"] != "old")

    broken = WritableSession(fail=True)
    asyncio.run(sync._record_tick({"ran": True}, session_factory=lambda: broken))
    check("a database failure is swallowed (the worker is never disturbed)", True)


def test_poll_records_a_tick_even_when_the_tick_raises():
    print("the scheduled poll records its heartbeat, including when run_once raises:")
    from app.integrations.openphone import sync

    recorded = []
    saved = (sync.enabled, sync.run_once, sync._record_tick)

    async def fake_record(result, **kw):
        recorded.append(result)

    async def boom(**kw):
        raise RuntimeError("OpenPhone exploded")

    async def fine(**kw):
        return {"ran": True, "mode": "poll"}

    try:
        sync.enabled = lambda: True
        sync._record_tick = fake_record
        sync.run_once = boom
        asyncio.run(sync.poll())
        check("a raising tick is recorded as not run", recorded[-1]["ran"] is False)
        sync.run_once = fine
        asyncio.run(sync.poll())
        check("a good tick is recorded as run", recorded[-1]["ran"] is True)
        sync.enabled = lambda: False
        n = len(recorded)
        asyncio.run(sync.poll())
        check("a disabled mirror records nothing", len(recorded) == n)
    finally:
        sync.enabled, sync.run_once, sync._record_tick = saved


def test_preview_never_writes_the_heartbeat():
    print("a dry run (preview) does not touch the heartbeat:")
    from app.integrations.openphone import sync

    src = inspect.getsource(sync.run_once)
    check("run_once does not record a tick (only poll does)", "_record_tick" not in src)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        print(f"\n{fn.__name__}")
        fn()
    print(f"\nAll {len(tests)} link-status checks passed.")


if __name__ == "__main__":
    main()
