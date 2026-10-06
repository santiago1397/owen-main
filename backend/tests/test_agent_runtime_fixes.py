"""Three bugs on the agent call path, fixed with the Retell work (RETELL-PLAN phase 1).

  1. **The backup agent-recording registration always raised.** `_register_agent_recording`
     called `queue.enqueue("recording_fetch", {...})` without the session — the signature is
     `enqueue(db, job_type, payload)` — so every call hit a TypeError, swallowed by its
     caller, and the backup path never queued anything. The fake below BINDS the real
     signature, so the old call fails this test; the fix queues the same payload the ARI
     consumer queues, and only while the recording has not been fetched yet.
  2. **Every agent transcript was stored as English.** `language="en"` was hardcoded; the
     language owen-voice reports (phase 4) is stored now, and an engine that reports none
     stores NULL — unknown, not a guess.
  3. **The dialled number never reached the caller-context lookup.** OWEN never sent
     `SessionIn.dialed_number`, and owen-voice looked for it inside `agent`, where pydantic
     drops it. OWEN now sends it at the top level (and owen-voice reads it from there —
     `owen-voice/tests/test_dialed_number.py`). The CRM lookup also gains C2's optional
     `agent_name`, and the request is byte-for-byte unchanged without one.
  4. **The Retell brief hop** returns the CRM's answer filtered to C2's keys by name.

Run: python -m tests.test_agent_runtime_fixes
"""

import asyncio
import inspect
import uuid
from types import SimpleNamespace

from tests import retell_guard
from tests.retell_support import CALLER, DID, LINKEDID, Patch, Settings
from tests.retell_support import check as _check


def check(name, cond):
    _check(name, cond, "agent_runtime_fixes")


class FakeSession:
    def __init__(self, call=None):
        self.added = []
        self.commits = 0
        self.call = call

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        self.commits += 1

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, stmt):
        call = self.call

        class R:
            def scalar_one_or_none(self_inner):
                return call
        return R()


def _register(storage_path=None):
    from app.flows import runtime
    from app.services import queue, recordings

    session = FakeSession()
    jobs = []
    real = inspect.signature(queue.enqueue)

    async def enqueue(*args, **kwargs):
        real.bind(*args, **kwargs)        # TypeError on the old call, exactly as in production
        jobs.append((args, kwargs))

    async def ingest(db, provider, rec):
        return SimpleNamespace(id=uuid.UUID("44444444-4444-4444-4444-444444444444"),
                               storage_path=storage_path)

    with Patch((runtime, "SessionLocal", lambda: session), (queue, "enqueue", enqueue),
               (recordings, "ingest_recording_event", ingest)):
        asyncio.run(runtime._register_agent_recording(1, LINKEDID, f"{LINKEDID}-agent-1"))
    return session, jobs


def test_the_backup_recording_registration_queues_a_fetch():
    print("registering an agent recording queues ONE recording_fetch, with the session:")
    session, jobs = _register()
    check("one job", len(jobs) == 1)
    args, kwargs = jobs[0]
    check("the session is the first argument (the bug: it was missing)", args[0] is session)
    check("a recording_fetch", args[1] == "recording_fetch")
    payload = args[2]
    check("for the registered row, with the consumer's own payload keys",
          payload["recording_id"] == "44444444-4444-4444-4444-444444444444"
          and payload["provider"] == "asterisk"
          and payload["recording_sid"].startswith(LINKEDID))

    print("a recording already fetched is not queued again:")
    _session, jobs = _register(storage_path="/data/recordings/x.wav")
    check("no job", jobs == [])


def _persist(data):
    from app.flows import runtime

    call = SimpleNamespace(id=uuid.uuid4(), number_id=None, direction="inbound",
                           started_at=None)
    db = FakeSession(call=call)
    asyncio.run(runtime._persist_agent_output(db, 1, LINKEDID, None, data))
    return [o for o in db.added if type(o).__name__ == "Transcription"]


def test_the_transcript_keeps_its_language():
    print("the transcript is stored in the language the recogniser reported:")
    seg = [{"speaker": "agent", "text": "Hola, Dream Team Roofing."}]
    rows = _persist({"transcript": seg, "language": "es"})
    check("one transcript row", len(rows) == 1)
    check("Spanish is stored as Spanish (the bug: always 'en')", rows[0].language == "es")
    check("by owen_voice when the engine does not say", rows[0].engine == "owen_voice")
    rows = _persist({"transcript": seg})
    check("no language reported -> NULL, not a guessed 'en'", rows[0].language is None)
    rows = _persist({"transcript": seg, "engine": "retell", "language": "en"})
    check("the engine is recorded when it says", rows[0].engine == "retell")


def test_the_dialled_number_reaches_owen_voice():
    print("OWEN sends the dialled number where owen-voice declares it:")
    import httpx

    from app.agents import remote
    from app.agents.session import AgentCallContext, AgentSpec

    sent = {}

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"port": "end_call", "data": {}}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            sent["body"] = json
            return FakeResponse()

    async def no_cap():
        return False

    with Settings(VOICE_SERVICE_URL="http://voice.invalid:9"), \
            Patch((httpx, "AsyncClient", FakeClient), (remote, "_over_spend_cap", no_cap)):
        ctx = AgentCallContext(channel_id="ch", linkedid=LINKEDID, caller_number=None,
                               dialed_number=DID)
        asyncio.run(remote.RemoteVoiceAgentSession().run(AgentSpec(agent_id="a"), ctx))
    check("top-level dialed_number, the field SessionIn declares",
          sent["body"]["dialed_number"] == DID)
    check("and not hidden in `agent`, where it was dropped",
          "dialed_number" not in sent["body"]["agent"])


def test_the_crm_lookup_body():
    print("the CRM caller-context request body:")
    from app.integrations.crm.client import CrmClient, CrmResult

    seen = []

    async def request(self, method, path, **kw):
        seen.append(kw.get("json"))
        return CrmResult(True, 200, "", {"known": False})

    with Patch((CrmClient, "_request", request)):
        c = CrmClient("http://crm.invalid", "ghl_pat_x")
        asyncio.run(c.agent_context(CALLER))
        asyncio.run(c.agent_context(CALLER, agent_name="Receptionist"))
    check("without an agent: exactly the number, as before", seen[0] == {"caller_number": CALLER})
    check("with one: C2's agent_name too",
          seen[1] == {"caller_number": CALLER, "agent_name": "Receptionist"})


def test_the_brief_hop_filters_to_c2():
    print("POST /api/agent-runtime/crm-link/brief returns the CRM answer, C2 keys only:")
    from app.api import agent_runtime
    from app.integrations.crm.client import CrmClient, CrmResult

    asked = []

    async def agent_context(self, caller_number, agent_name=""):
        asked.append((caller_number, agent_name))
        return CrmResult(True, 200, "", {"known": True, "source": "crm",
                                         "contact": {"first_name": "Maria"},
                                         "address": "12 Palm Ave, Bradenton",
                                         "value_cents": 950000, "notes": "secret"})

    body = agent_runtime.LookupIn(caller_number=CALLER, dialed_number=DID,
                                  agent_name="Receptionist")
    with Settings(CRM_LINK_ENABLED=True, CRM_LINK_TOKEN="ghl_pat_x",
                  CRM_LINK_BASE_URL="http://crm.invalid"), \
            Patch((CrmClient, "agent_context", agent_context)):
        out = asyncio.run(agent_runtime.crm_link_brief(body, _key=None))
    check("the agent's name was passed to the CRM", asked == [(CALLER, "Receptionist")])
    check("C2's keys come back", out["answer"]["address"] == "12 Palm Ave, Bradenton"
          and out["answer"]["contact"] == {"first_name": "Maria"})
    check("anything else is dropped, whatever it is called",
          "value_cents" not in out["answer"] and "notes" not in out["answer"])

    with Settings(CRM_LINK_ENABLED=False), Patch((CrmClient, "agent_context", agent_context)):
        out = asyncio.run(agent_runtime.crm_link_brief(body, _key=None))
    check("link off: {} and the CRM is not asked", out == {} and len(asked) == 1)


if __name__ == "__main__":
    test_the_backup_recording_registration_queues_a_fetch()
    test_the_transcript_keeps_its_language()
    test_the_dialled_number_reaches_owen_voice()
    test_the_crm_lookup_body()
    test_the_brief_hop_filters_to_c2()
    retell_guard.assert_untouched()
    print("\nALL AGENT RUNTIME FIX CHECKS PASSED")
