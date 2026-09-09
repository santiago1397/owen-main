"""Pure tests for the streaming-STT turn state machine — no Deepgram, no socket, no audio.

This is the logic VOICE_STACK_MIGRATION M1/M2 adds, and it is exactly the kind that is
invisible when it is wrong: an eager draft spoken too early is the agent talking over a
caller, and a dead socket reported as a graceful ending is a customer sent to the wrong
place. Neither raises an exception. So the state machine is driven here against a FAKE STT,
in the same spirit as test_audiosocket.py and the backend's flow interpreter.

The rule under test, stated once: a turn may be GENERATED on a predicted end-of-turn, but
nothing may be SPOKEN until the turn is committed.

Run:  python -m tests.test_flux_turns      (from owen-voice/)
"""

import asyncio
import sys
import types

sys.path.insert(0, ".")

# The pipeline imports httpx (providers) at module load; we exercise no I/O, so stub it
# rather than requiring the dependency to run a pure test.
for _m in ("httpx", "websockets"):
    if _m not in sys.modules:
        _mod = types.ModuleType(_m)
        _mod.Timeout = lambda *a, **k: None
        _mod.AsyncClient = object
        sys.modules[_m] = _mod

from app.providers import TurnEvent  # noqa: E402
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


class FakeSTT:
    """Stands in for DeepgramFluxSTT: a queue the test pushes turn events onto."""

    name = "fake"

    def __init__(self) -> None:
        self.events: asyncio.Queue = asyncio.Queue()
        self.fed = bytearray()
        self.closed = False

    async def start(self) -> bool:
        return True

    async def feed(self, pcm: bytes) -> None:
        self.fed.extend(pcm)

    async def close(self) -> None:
        self.closed = True

    def emit(self, kind: str, transcript: str = "", conf: float = 0.0) -> None:
        self.events.put_nowait(TurnEvent(kind, transcript, conf))


def build_convo(stt: FakeSTT):
    """A Conversation with the pump's collaborators replaced by recorders."""
    from app.pipeline import Conversation

    session = MediaSession(session_uuid="test")
    convo = Conversation.__new__(Conversation)     # skip __init__'s provider construction
    convo.session = session
    convo.stream_stt = stt
    convo._turn = None
    convo._draft = None
    convo._draft_for = ""
    convo._commit = asyncio.Event()
    convo._commit.set()
    convo._started = 0.0
    convo._last_voice = 0.0
    convo._stt_pump = None

    started: list = []

    class FakePlayout:
        def __init__(self):
            self.cleared = 0

        def clear(self) -> int:
            self.cleared += 1
            return 3

    convo.playout = FakePlayout()

    def _begin_turn(text: str):
        started.append(text)

        async def _noop():
            await asyncio.sleep(3600)      # a turn that is "in flight" until cancelled

        convo._turn = asyncio.get_event_loop().create_task(_noop())
        return convo._turn

    convo._begin_turn = _begin_turn
    return convo, started


async def drain() -> None:
    """Let the pump process everything queued."""
    for _ in range(6):
        await asyncio.sleep(0)


async def scenario_eager_then_commit() -> None:
    print("\n[eager end-of-turn, then committed]")
    stt = FakeSTT()
    convo, started = build_convo(stt)
    pump = asyncio.create_task(convo._pump_turn_events())

    stt.emit("eager_end", "I need a quote for a roof leak")
    await drain()
    check(started == ["I need a quote for a roof leak"], "draft turn starts on eager_end")
    check(not convo._commit.is_set(), "playback gate is CLOSED while only predicted")

    stt.emit("end", "I need a quote for a roof leak")
    await drain()
    check(convo._commit.is_set(), "gate opens on the committed EndOfTurn")
    check(started == ["I need a quote for a roof leak"], "the draft is reused, not restarted")
    check(convo.session.eager_hits == 1, "an eager hit is counted")

    pump.cancel()


async def scenario_eager_then_resumed() -> None:
    print("\n[eager end-of-turn, then the caller carries on]")
    stt = FakeSTT()
    convo, started = build_convo(stt)
    pump = asyncio.create_task(convo._pump_turn_events())

    stt.emit("eager_end", "I need a quote")
    await drain()
    draft = convo._turn
    check(not convo._commit.is_set(), "gate closed on the prediction")

    stt.emit("resumed")
    await drain()
    check(draft.cancelled() or draft.done(), "the draft turn is abandoned on TurnResumed")
    check(convo.session.eager_retracted == 1, "a retraction is counted")

    # The real turn, when it lands, is a fresh one with the full sentence.
    stt.emit("end", "I need a quote for a roof leak on the back porch")
    await drain()
    check(started[-1] == "I need a quote for a roof leak on the back porch",
          "the committed turn uses the FULL transcript, not the truncated prediction")
    check(convo._commit.is_set(), "gate is open for the real turn")

    pump.cancel()


async def scenario_transcript_mismatch() -> None:
    print("\n[eager transcript does not match the committed one]")
    stt = FakeSTT()
    convo, started = build_convo(stt)
    pump = asyncio.create_task(convo._pump_turn_events())

    stt.emit("eager_end", "cancel my appointment")
    await drain()
    draft = convo._turn

    # Deepgram guarantees these match; if that contract ever breaks we must NOT speak a reply
    # written for something the caller did not say.
    stt.emit("end", "confirm my appointment")
    await drain()
    check(draft.cancelled() or draft.done(), "a mismatched draft is discarded")
    check(started[-1] == "confirm my appointment", "the turn is regenerated from the truth")

    pump.cancel()


async def scenario_plain_end() -> None:
    print("\n[no eager event at all — plain EndOfTurn]")
    stt = FakeSTT()
    convo, started = build_convo(stt)
    pump = asyncio.create_task(convo._pump_turn_events())

    stt.emit("end", "hello there")
    await drain()
    check(started == ["hello there"], "a turn starts on EndOfTurn alone")
    check(convo._commit.is_set(), "gate open — nothing was speculative")

    stt.emit("end", "")
    await drain()
    check(started == ["hello there"], "an empty transcript starts no turn")

    pump.cancel()


async def scenario_barge_in() -> None:
    print("\n[caller interrupts]")
    stt = FakeSTT()
    convo, started = build_convo(stt)
    pump = asyncio.create_task(convo._pump_turn_events())

    stt.emit("end", "tell me about your prices")
    await drain()
    speaking = convo._turn

    stt.emit("start")
    await drain()
    check(convo.playout.cleared == 1, "queued audio is dropped on StartOfTurn")
    check(speaking.cancelled() or speaking.done(), "the in-flight answer is abandoned")
    check(convo.session.vad_starts == 1, "the interruption is counted")

    pump.cancel()


async def scenario_stream_death() -> None:
    print("\n[the STT socket dies mid-call]")
    stt = FakeSTT()
    convo, started = build_convo(stt)
    pump = asyncio.create_task(convo._pump_turn_events())

    stt.emit("error")
    await drain()
    check(convo.session.stt_failed, "the failure is recorded on the session")
    check(convo.session.result_port == "failed",
          "the port is PINNED to `failed` so the flow routes to voicemail (M6)")
    check(convo.session.error, "an error string is set, so close() cannot report success")
    check(convo._commit.is_set(), "no turn is left parked on the gate")
    check(convo._guardrail() == "stt_stream_failed",
          "the next frame ends the call instead of waiting 30s for the silence guardrail")
    check(pump.done(), "the pump stops rather than spinning on a dead queue")


async def main() -> None:
    await scenario_eager_then_commit()
    await scenario_eager_then_resumed()
    await scenario_transcript_mismatch()
    await scenario_plain_end()
    await scenario_barge_in()
    await scenario_stream_death()

    print(f"\n{_checks} checks, {len(_failures)} failed")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1 if _failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
