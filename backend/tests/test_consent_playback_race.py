"""Consent/prompt playback must survive a playback that ends INSTANTLY (regression for the
2026-08-03 live failure).

THE BUG, as it played out live: `OUTBOUND_CONSENT_MEDIA` defaulted to
`sound:owen/outbound-recording-consent`, a sound name that was never provisioned on the
Asterisk host. Asterisk logged "File owen/outbound-recording-consent does not exist in any
format" and published PlaybackFinished(failed) at essentially the same instant it answered
the /play POST. But the completion queue was only registered AFTER that POST returned, so
`push_playback` found no waiter and dropped the event. `_play_consent_or_leg_gone` then sat
on the notice for its full 30s cap with the operator NOT yet bridged — dead air on every
outbound call. On the 16:16 call the callee was one of our own flow-assigned DIDs, whose IVR
menu timed out and hung up after 11s, so the call died as `hangup_during_consent` before the
operator ever heard anything.

The fix is `_start_playback`: assign the playback id client-side and register the queue
BEFORE calling ARI, exactly as `_originate_with_id` pre-assigns channel ids.

Asserts, for both playback waiters:
- the completion queue is registered before ARI is called (the push finds a waiter);
- an instantly-finished playback returns at once instead of burning the timeout;
- consent returning True means the caller goes on to BRIDGE rather than tearing down.

Network is stubbed at _resolve_media/_post_json, so this is stdlib-only apart from the
client import. Run: python -m tests.test_consent_playback_race
"""

import asyncio
import time

from app.flows import dtmf
from app.providers.asterisk_client import AsteriskAriClient

CHAN = "0978453fdbb24cd3aa827920a6dfa3ab"   # the live callee leg
OP_CHAN = "af6bc634b6504c749721e34c760cff7a"  # the live operator leg


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"consent_playback_race failed at: {name}")


class InstantFailAri(AsteriskAriClient):
    """Asterisk answering /play for a media it cannot open: the PlaybackFinished lands while
    the POST is still being awaited, which is precisely the window the old code left open."""

    def __init__(self):  # noqa: D107 - deliberately skips the real __init__ (no settings/httpx)
        self.delivered: bool | None = None
        self.playback_id: str = ""

    async def _resolve_media(self, media):
        return str(media)

    async def _post_json(self, path, params=None, json=None):
        params = params or {}
        self.playback_id = str(params.get("playbackId") or "")
        # ARI requires a playbackId we can key the event on; without one there is nothing to
        # register up front and the race is unavoidable.
        if not self.playback_id:
            self.delivered = False
            return {"id": "server-assigned"}
        self.delivered = dtmf.push_playback(self.playback_id, {
            "type": "PlaybackFinished",
            "playback": {"id": self.playback_id, "state": "failed"},
        })
        return {"id": self.playback_id}


def test_consent_notice_does_not_dead_air_on_unplayable_media():
    print("consent notice — unplayable media proceeds to the bridge at once, not after 30s:")

    async def scenario():
        ari = InstantFailAri()
        started = time.monotonic()
        proceed = await ari._play_consent_or_leg_gone(
            asyncio.Queue(), CHAN, "sound:owen/outbound-recording-consent",
            {OP_CHAN, CHAN}, timeout_s=5.0,
        )
        elapsed = time.monotonic() - started
        check("completion queue was registered BEFORE ARI was called", ari.delivered is True)
        check("consent returns True -> caller goes on to bridge", proceed is True)
        check("returned immediately, did not burn the cap", elapsed < 1.0)
        check("no playback registration leaked", dtmf._playbacks.get(ari.playback_id) is None)

    asyncio.run(scenario())


def test_play_and_wait_returns_on_instant_finish():
    print("play_and_wait — an instantly-finished prompt returns at once:")

    async def scenario():
        ari = InstantFailAri()
        started = time.monotonic()
        await ari.play_and_wait(CHAN, "sound:owen/outbound-recording-consent", timeout_s=5.0)
        elapsed = time.monotonic() - started
        check("completion queue was registered BEFORE ARI was called", ari.delivered is True)
        check("returned immediately, did not burn the cap", elapsed < 1.0)
        check("no playback registration leaked", dtmf._playbacks.get(ari.playback_id) is None)

    asyncio.run(scenario())


def test_menu_prompt_returns_on_instant_finish():
    print("menu prompt — an instantly-finished prompt falls straight through to the digit timer:")

    async def scenario():
        ari = InstantFailAri()
        started = time.monotonic()
        got = await ari._play_prompt_barge_in(
            CHAN, "sound:owen/outbound-recording-consent", asyncio.Queue(), timeout_s=5.0,
        )
        elapsed = time.monotonic() - started
        check("completion queue was registered BEFORE ARI was called", ari.delivered is True)
        check("no barge-in digit reported", got == "")
        check("returned immediately, did not burn the cap", elapsed < 1.0)

    asyncio.run(scenario())


def test_refused_playback_is_not_awaited():
    print("ARI refusing the play (no body) leaves nothing registered and does not block:")

    class RefusingAri(InstantFailAri):
        async def _post_json(self, path, params=None, json=None):
            self.playback_id = str((params or {}).get("playbackId") or "")
            return None

    async def scenario():
        ari = RefusingAri()
        started = time.monotonic()
        proceed = await ari._play_consent_or_leg_gone(
            asyncio.Queue(), CHAN, "sound:missing", {OP_CHAN, CHAN}, timeout_s=5.0,
        )
        check("consent proceeds rather than stalling", proceed is True)
        check("returned immediately", time.monotonic() - started < 1.0)
        check("registration cleaned up", dtmf._playbacks.get(ari.playback_id) is None)

    asyncio.run(scenario())


def test_consent_media_default_is_synthesizable():
    print("the shipped OUTBOUND_CONSENT_MEDIA default does not depend on a provisioned file:")
    from app.core.config import Settings

    media = Settings.model_fields["OUTBOUND_CONSENT_MEDIA"].default
    check("default is not a bare sound: name", not str(media).startswith("sound:"))
    check("default is prompt text TTS can synthesize", " " in str(media) and len(str(media)) > 10)


if __name__ == "__main__":
    test_consent_notice_does_not_dead_air_on_unplayable_media()
    test_play_and_wait_returns_on_instant_finish()
    test_menu_prompt_returns_on_instant_finish()
    test_refused_playback_is_not_awaited()
    test_consent_media_default_is_synthesizable()
    print("\nALL CONSENT PLAYBACK RACE CHECKS PASSED")
