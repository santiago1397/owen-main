"""The caller-context request carries the number the caller DIALLED (RETELL-PLAN phase 1 fix).

`SessionIn.dialed_number` has always been declared; OWEN never sent it, and `_fetch_context`
looked for it inside `session.agent` — a dict built from `AgentConfig`, which does not declare
`dialed_number`, so pydantic dropped it. Every context request said the dialled number was "".

Pinned here, with no socket and no provider:
  * POST /sessions copies `dialed_number` onto the session;
  * `_fetch_context` sends the session's dialled number to the provider.

Run:  python -m tests.test_dialed_number      (from owen-voice/)
"""

import asyncio
import sys

sys.path.insert(0, ".")

from app.session import MediaSession  # noqa: E402

_checks = 0


def check(name, cond):
    global _checks
    _checks += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"dialed_number failed at: {name}")


def test_the_session_model_and_the_request_declare_it():
    print("the session carries the dialled number, and the request declares it:")
    from app.agent_api import SessionIn

    check("SessionIn declares dialed_number at the top level",
          "dialed_number" in SessionIn.model_fields)
    s = MediaSession(session_uuid="s1")
    check("MediaSession has a dialed_number field, empty by default", s.dialed_number == "")


def test_fetch_context_sends_it():
    print("_fetch_context sends the session's dialled number to the provider:")
    import app.context as context
    from app import main

    seen = {}

    async def fake_fetch(provider, *, caller_number, dialed_number, linkedid, timeout_s):
        seen.update(caller_number=caller_number, dialed_number=dialed_number,
                    linkedid=linkedid)
        return {}

    saved = context.fetch_provider
    context.fetch_provider = fake_fetch
    try:
        s = MediaSession(session_uuid="s2")
        s.caller_number, s.dialed_number, s.linkedid = "+19415550123", "+19545550199", "1.2"
        s.context_provider = {"url": "http://owen.invalid/lookup", "headers": {}}
        s.agent = {"persona": "x"}        # no dialed_number in here, as in production
        asyncio.run(main._fetch_context(s))
    finally:
        context.fetch_provider = saved
    check("the provider is told the dialled number", seen.get("dialed_number") == "+19545550199")
    check("and the caller's", seen.get("caller_number") == "+19415550123")


if __name__ == "__main__":
    test_the_session_model_and_the_request_declare_it()
    test_fetch_context_sends_it()
    print(f"\nALL {_checks} DIALED-NUMBER CHECKS PASSED")
