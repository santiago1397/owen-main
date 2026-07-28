"""Menu prompt timing + barge-in for AsteriskAriClient.read_digit (regression for the
2026-07-28 live failure).

THE BUG: `play()` only fires the ARI request and returns, so read_digit started the
first-digit timer the moment the prompt STARTED. A menu whose prompt was longer than its
timeout hung up on every caller who listened to the options — observed live on the "test"
flow: a 6.11s prompt on a node with the default 5s timeout took the 'timeout' port 5.0s
after answer, cutting the caller off mid-word.

Asserts:
- the digit timer starts only AFTER the prompt finishes (a prompt longer than the timeout
  no longer eats the caller's chance to answer) — the regression that motivated the fix;
- barge-in still works: a digit pressed DURING the prompt is returned and stops playback;
- silence after the prompt still returns None (-> the menu's 'timeout' port);
- multi-digit collection still works across the prompt boundary;
- an unregistered channel still plays the prompt and behaves like a timeout.

Network is stubbed at the client's _resolve_media/_post_json/_delete seams, so this is
stdlib-only apart from the client import. Run: python -m tests.test_menu_prompt_timing
"""

import asyncio
import time

from app.flows import dtmf
from app.providers.asterisk_client import AsteriskAriClient

CHAN = "1785263998.9"
PB_ID = "pb-1"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"menu_prompt_timing failed at: {name}")


class StubAri(AsteriskAriClient):
    """The real read_digit / _play_prompt_barge_in over stubbed HTTP seams."""

    def __init__(self):  # noqa: D107 - deliberately skips the real __init__ (no settings/httpx)
        self.played: list[str] = []
        self.deleted: list[str] = []

    async def _resolve_media(self, media):
        return f"sound:{media}"

    async def _post_json(self, path, params=None, json=None):
        self.played.append((params or {}).get("media"))
        return {"id": PB_ID}

    async def _post(self, path, params=None):
        self.played.append((params or {}).get("media"))
        return True

    async def _delete(self, path):
        self.deleted.append(path)
        return True


async def _finish_prompt_after(delay: float) -> None:
    await asyncio.sleep(delay)
    dtmf.push_playback(PB_ID, {"type": "PlaybackFinished"})


async def _press_after(delay: float, digit: str) -> None:
    await asyncio.sleep(delay)
    dtmf.push_digit(CHAN, digit)


def test_timer_starts_after_prompt():
    """THE regression, scaled like the live failure: the prompt (1.2s) is LONGER than the
    node timeout (0.6s) and the caller answers 0.2s after it ends. Old code: timed out at
    0.6s -> 'timeout' port -> hangup. Fixed: the digit is collected.

    NB timings must stay above read_digit's 0.5s timeout floor (`max(0.5, ...)`), or the
    clamp — not the fix — is what makes this pass."""
    print("digit timer starts only after the prompt finishes:")

    async def scenario():
        dtmf.register_digits(CHAN)
        ari = StubAri()
        try:
            asyncio.ensure_future(_finish_prompt_after(1.2))
            asyncio.ensure_future(_press_after(1.4, "1"))
            started = time.monotonic()
            got = await ari.read_digit(CHAN, prompt="press 1 or 2", timeout_s=0.6, max_digits=1)
            elapsed = time.monotonic() - started
            check("digit collected despite prompt > timeout", got == "1")
            check("waited past the prompt (not the old 0.6s cut-off)", elapsed >= 1.35)
            check("prompt was played once", len(ari.played) == 1)
            check("playback not stopped (no barge-in)", ari.deleted == [])
        finally:
            dtmf.unregister_digits(CHAN)

    asyncio.run(scenario())


def test_barge_in_stops_prompt():
    print("barge-in — a digit during the prompt returns immediately and stops playback:")

    async def scenario():
        dtmf.register_digits(CHAN)
        ari = StubAri()
        try:
            asyncio.ensure_future(_finish_prompt_after(5.0))  # long prompt, never reached
            asyncio.ensure_future(_press_after(0.05, "2"))
            started = time.monotonic()
            got = await ari.read_digit(CHAN, prompt="press 1 or 2", timeout_s=0.6, max_digits=1)
            elapsed = time.monotonic() - started
            check("barge-in digit returned", got == "2")
            check("returned without waiting out the prompt", elapsed < 1.0)
            check("playback stopped", ari.deleted == [f"/ari/playbacks/{PB_ID}"])
        finally:
            dtmf.unregister_digits(CHAN)

    asyncio.run(scenario())


def test_silence_after_prompt_times_out():
    print("silence after the prompt still yields None (-> 'timeout' port):")

    async def scenario():
        dtmf.register_digits(CHAN)
        ari = StubAri()
        try:
            asyncio.ensure_future(_finish_prompt_after(0.30))
            got = await ari.read_digit(CHAN, prompt="press 1 or 2", timeout_s=0.6, max_digits=1)
            check("no input -> None", got is None)
        finally:
            dtmf.unregister_digits(CHAN)

    asyncio.run(scenario())


def test_multi_digit_across_prompt_boundary():
    print("multi-digit collection spans the prompt boundary:")

    async def scenario():
        dtmf.register_digits(CHAN)
        ari = StubAri()
        try:
            asyncio.ensure_future(_press_after(0.05, "4"))       # barge-in
            asyncio.ensure_future(_press_after(0.30, "2"))       # after the stop
            asyncio.ensure_future(_finish_prompt_after(5.0))     # superseded by barge-in
            got = await ari.read_digit(CHAN, prompt="extension?", timeout_s=0.8, max_digits=2)
            check("both digits collected in order", got == "42")
        finally:
            dtmf.unregister_digits(CHAN)

    asyncio.run(scenario())


def test_unregistered_channel_still_plays():
    print("unregistered channel — prompt still played, behaves like a timeout:")

    async def scenario():
        ari = StubAri()
        got = await ari.read_digit("no-such-chan", prompt="hello", timeout_s=0.1, max_digits=1)
        check("returns None", got is None)
        check("prompt still played", len(ari.played) == 1)

    asyncio.run(scenario())


if __name__ == "__main__":
    test_timer_starts_after_prompt()
    test_barge_in_stops_prompt()
    test_silence_after_prompt_times_out()
    test_multi_digit_across_prompt_boundary()
    test_unregistered_channel_still_plays()
    print("\nALL MENU PROMPT TIMING CHECKS PASSED")
