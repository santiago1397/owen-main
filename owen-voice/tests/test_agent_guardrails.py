"""The call-length and silence guardrails, per AGENT — not per server.

Why this exists: OWEN sends `max_call_seconds` / `max_silence_seconds` from the pinned
agent version on every session (`backend/app/agents/remote.py`), and owen-voice accepted
them and used them for ONE thing — sizing the HTTP timeout. The guardrail that actually
ends the call read the env instead, so an agent configured to hang up after 60 seconds ran
to the env's 300. Nothing raised; the operator just watched their setting do nothing, which
is the kind of failure a unit test catches and a live call does not.

The rule under test: the agent's own value wins, the env is the fallback, 0 means no limit,
and rubbish falls back rather than disarming the guardrail.

Run:  python -m tests.test_agent_guardrails      (from owen-voice/)
"""

import sys
import types

sys.path.insert(0, ".")

# Same stubs as test_flux_turns: the pipeline imports httpx/websockets at module load and
# this test does no I/O at all.
for _m in ("httpx", "websockets"):
    if _m not in sys.modules:
        _mod = types.ModuleType(_m)
        _mod.Timeout = lambda *a, **k: None
        _mod.AsyncClient = object
        sys.modules[_m] = _mod

from app.config import settings  # noqa: E402
from app.session import MediaSession  # noqa: E402

_checks = 0
_failures = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        _failures.append(label)
        print(f"  FAIL  {label}")
    else:
        print(f"  ok    {label}")


def build_convo(agent: dict, *, elapsed: float = 0.0, silent: float = 0.0):
    """A Conversation with no providers: only the guardrail is exercised.

    `elapsed` and `silent` are seconds in the past, expressed the way the pipeline holds
    them — monotonic stamps — so the test never sleeps.
    """
    import time

    from app.pipeline import Conversation

    convo = Conversation.__new__(Conversation)     # skip __init__'s provider construction
    convo.session = MediaSession(session_uuid="test")
    convo.session.stt_failed = False
    convo.agent = agent
    now = time.monotonic()
    convo._started = now - elapsed
    convo._last_voice = now - silent
    return convo


def main() -> int:
    env_call = settings.AGENT_MAX_CALL_SECONDS
    env_silence = settings.AGENT_MAX_SILENCE_SECONDS
    print(f"[env defaults: max_call={env_call}s max_silence={env_silence}s]")

    print("\n[an agent's own limit is shorter than the env's]")
    convo = build_convo({"max_call_seconds": 60}, elapsed=61)
    check(convo._guardrail() == "max_call_seconds",
          "a 60s agent ends at 61s even though the env allows 300s")
    convo = build_convo({"max_call_seconds": 60}, elapsed=59)
    check(convo._guardrail() is None, "...and not a second before its own limit")

    print("\n[silence, the same way]")
    convo = build_convo({"max_silence_seconds": 5}, silent=6)
    check(convo._guardrail() == "max_silence_seconds",
          "a 5s silence limit fires at 6s of quiet")
    convo = build_convo({"max_silence_seconds": 5}, silent=4)
    check(convo._guardrail() is None, "...and stays quiet below it")

    print("\n[no per-agent value: the env still governs]")
    convo = build_convo({}, elapsed=env_call + 1)
    check(convo._guardrail() == "max_call_seconds",
          "an agent that sets nothing is ended by the env default")
    convo = build_convo({"max_call_seconds": None}, elapsed=env_call + 1)
    check(convo._guardrail() == "max_call_seconds",
          "an explicit null is the same as absent (it is what OWEN sends)")

    print("\n[an agent may turn a guardrail OFF]")
    convo = build_convo({"max_call_seconds": 0}, elapsed=env_call + 10_000)
    check(convo._guardrail() is None,
          "0 means no limit, and the env default does not creep back in")

    print("\n[a longer agent limit than the env's is honoured, not clamped]")
    convo = build_convo({"max_call_seconds": env_call + 120}, elapsed=env_call + 1)
    check(convo._guardrail() is None,
          "an agent allowed longer than the env keeps talking")

    print("\n[rubbish falls back rather than disarming the guardrail]")
    convo = build_convo({"max_call_seconds": "not a number"}, elapsed=env_call + 1)
    check(convo._guardrail() == "max_call_seconds",
          "an unparseable value uses the env default")
    convo = build_convo({"max_call_seconds": -5}, elapsed=env_call + 1)
    check(convo._guardrail() == "max_call_seconds",
          "a negative value uses the env default")

    print("\n[a dead STT stream still wins over everything]")
    convo = build_convo({"max_call_seconds": 0})
    convo.session.stt_failed = True
    check(convo._guardrail() == "stt_stream_failed",
          "the stream failure is reported even with every limit off")

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    for f in _failures:
        print(f"  FAILED: {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
