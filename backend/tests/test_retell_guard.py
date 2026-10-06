"""The guard that keeps every test away from Retell, proved — and the brief it would protect.

  1. **api.retellai.com and sip.retellai.com are refused** at the socket layer, by a raw
     lookup and by a real `httpx` client with no mock transport, and every attempt is
     RECORDED — so `assert_untouched()` fails a module whose code swallowed the refusal.
     A host that is not Retell's is not affected.
  2. **A forgotten mock is caught.** The engine run with a key and NO mocked transport tries
     the real API; the guard refuses it, the engine takes `failed` (as it must for any
     outage), and the attempt is on the record.
  3. **The brief** (C2 -> Retell dynamic variables): the disclosure rule first, always; the
     address marked "never read aloud"; Zuper job, recent calls and texts rendered and capped;
     a key the CRM was never supposed to send is never read; no name -> unknown.

Run: python -m tests.test_retell_guard
"""

import asyncio
import socket

import httpx

from tests import retell_guard
from tests.retell_support import (CALLER, DID, KEY, FakeAri, MemoryRegistry, Patch,
                                  Settings)
from tests.retell_support import check as _check


def check(name, cond):
    _check(name, cond, "retell_guard")


def _refused_lookup(host):
    try:
        socket.getaddrinfo(host, 443)
    except retell_guard.RetellRefused:
        return True
    return False


def test_the_guard_refuses_retell_and_records_it():
    print("the guard refuses Retell's hosts and records every attempt:")
    retell_guard.attempts.clear()
    check("api.retellai.com is refused", _refused_lookup("api.retellai.com"))
    check("sip.retellai.com is refused", _refused_lookup("sip.retellai.com"))
    check("any *.retellai.com is refused", _refused_lookup("dashboard.retellai.com"))
    check("and each attempt is recorded", len(retell_guard.attempts) == 3)

    async def real_request():
        async with httpx.AsyncClient(timeout=2) as c:
            await c.post("https://api.retellai.com/v2/register-phone-call", json={})

    try:
        asyncio.run(real_request())
        reached = True
    except Exception:  # noqa: BLE001 - httpx wraps the refusal as ConnectError
        reached = False
    check("a real httpx request to the API never connects", not reached)
    check("...and is recorded", "api.retellai.com" in retell_guard.attempts)

    with Settings(RETELL_API_BASE="https://retell-proxy.example.test"):
        check("whatever host RETELL_API_BASE names is refused too",
              _refused_lookup("retell-proxy.example.test"))
    check("a host that is not Retell's passes through",
          not retell_guard.refused("localhost") and not retell_guard.refused("retellai.com.evil"))
    retell_guard.attempts.clear()


def test_any_test_module_that_tries_fails():
    print("a module that tries to reach Retell — and swallows the refusal — still fails:")
    import os
    import subprocess
    import sys

    code = ("import socket, tests\n"
            "try:\n    socket.getaddrinfo('api.retellai.com', 443)\n"
            "except OSError:\n    pass\n"
            "print('module finished normally')\n")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run = subprocess.run([sys.executable, "-c", code], cwd=here, capture_output=True,
                         text=True, timeout=60)
    check("the guard is installed just by importing the tests package (every module does)",
          "module finished normally" in run.stdout)
    check("...and the process exits non-zero naming the attempt",
          run.returncode == 1 and "RETELL GUARD" in run.stdout)
    ok = subprocess.run([sys.executable, "-c", "import tests; print('fine')"], cwd=here,
                        capture_output=True, text=True, timeout=60)
    check("a module that never tries exits cleanly", ok.returncode == 0)


def test_a_forgotten_mock_is_caught():
    print("the engine with a key and NO mocked transport:")
    from app.agents import retell as engine
    from app.agents import spend
    from app.agents.session import AgentCallContext, AgentSpec
    from app.integrations.retell import client, registry

    async def no_cap():
        return False

    async def no_brief(_s, _c):
        return None

    retell_guard.attempts.clear()
    previous = registry.use(MemoryRegistry())
    ari = FakeAri()
    try:
        with Settings(RETELL_API_KEY=KEY, RETELL_API_BASE="https://api.retellai.com"), \
                Patch((client, "TRANSPORT", None), (spend, "over_cap", no_cap),
                      (engine, "_fetch_brief", no_brief)):
            result = asyncio.run(engine.RetellVoiceAgentSession().run(
                AgentSpec(agent_id="a", engine="retell",
                          config={"retell_agent_id": "agent_x"}),
                AgentCallContext(channel_id="c", linkedid="l", caller_number=CALLER, ari=ari,
                                 dialed_number=DID)))
    finally:
        registry.use(previous)
    check("the engine took the failed port (voicemail), as for any outage",
          result.port == "failed")
    check("no SIP leg was dialled", ari.ops == [])
    check("the guard recorded the attempt — the module would fail on it",
          set(retell_guard.attempts) == {"api.retellai.com"})
    retell_guard.attempts.clear()


def test_the_brief():
    print("the caller brief as Retell dynamic variables:")
    from datetime import datetime, timezone

    from app.integrations.retell.brief import (DISCLOSURE_RULE, MAX_CALL_SUMMARY,
                                               render_variables)

    now = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)
    answer = {
        "known": True, "source": "crm",
        "contact": {"first_name": "Maria", "last_name": "Ruiz"},
        "opportunity": {"title": "Ruiz roof leak", "stage": "Inspection", "pipeline": "AHS"},
        "next_appointment": {"starts_at": "2026-10-07T13:00:00Z", "title": "Inspection"},
        "zuper_job": {"job_number": "4471", "board": "AHS", "status": "Scheduled",
                      "status_since": "2026-10-01T12:00:00Z", "technician": "Luis",
                      "scheduled_start": "2026-10-07T13:00:00Z"},
        "recent_calls": [{"at": "2026-10-05T15:00:00Z", "channel": "quo",
                          "direction": "inbound", "summary": "S" * 900}] * 5,
        "recent_texts": [{"at": "2026-10-05T16:00:00Z", "direction": "outbound",
                          "text": "See you Tuesday"}],
        "address": "12 Palm Ave, Bradenton",
        "value_cents": 950000, "invoice_total": "$9,500", "notes": "gate code 1234",
    }
    v = render_variables(answer, caller_number=CALLER, dialed_number=DID, now=now)
    b = v["customer_brief"]
    check("the rule comes FIRST", b.startswith(DISCLOSURE_RULE))
    check("the rule forbids money and reading the address",
          "money" in DISCLOSURE_RULE and "Never read the address aloud" in DISCLOSURE_RULE)
    check("history only after the street address is confirmed",
          "ONLY after the caller has confirmed their street address" in DISCLOSURE_RULE)
    check("the address is there, marked for comparison only",
          "FOR COMPARISON ONLY, never read it aloud: 12 Palm Ave, Bradenton" in b)
    check("the Zuper job, technician and schedule",
          "Zuper job #4471 on AHS" in b and "technician Luis" in b and "status Scheduled" in b)
    check("a visit spoken in Bradenton's time (13:00Z is 9:00 AM)", "9:00 AM" in b)
    check("at most three recent calls, each summary capped",
          b.count("they called us") == 3 and ("S" * (MAX_CALL_SUMMARY + 1)) not in b)
    check("the recent text", 'we wrote: "See you Tuesday"' in b)
    check("no money and no notes, whatever the CRM sent",
          "9500" not in b and "9,500" not in b and "gate code" not in b)
    check("known, first name", v["customer_known"] == "yes" and v["customer_first_name"] == "Maria")

    nameless = render_variables({"known": True, "contact": {}, "address": "x"},
                                caller_number=CALLER, dialed_number=DID)
    check("a known caller with no name is treated as unknown — nothing about anybody",
          nameless["customer_known"] == "no" and "x" not in nameless["customer_brief"]
          .split("\n\n", 1)[1])
    junk = render_variables("not a dict", caller_number=CALLER, dialed_number=DID)
    check("a malformed answer is unknown, rule still first",
          junk["customer_known"] == "no" and junk["customer_brief"].startswith(DISCLOSURE_RULE))
    zuper = render_variables({"known": True, "source": "zuper",
                              "contact": {"first_name": "Ann", "last_name": "Lee"}},
                             caller_number=CALLER, dialed_number=DID)
    check("a Zuper-only customer says where they came from", "(from Zuper)" in zuper["customer_brief"])


if __name__ == "__main__":
    test_the_guard_refuses_retell_and_records_it()
    test_any_test_module_that_tries_fails()
    test_a_forgotten_mock_is_caught()
    test_the_brief()
    retell_guard.assert_untouched()
    print("\nALL RETELL GUARD CHECKS PASSED")
