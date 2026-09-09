"""Per-call media sessions + the UUID registry that correlates them to TCP connections.

A session is created BEFORE the externalMedia channel exists (we mint the UUID, then hand it
to ARI), so the registry is what lets an inbound TCP connection find the call it belongs to
when Asterisk sends its opening UUID frame.

Counters are not decoration. When someone reports "I heard nothing", the question is which
half failed, and these answer it without a packet capture:

    rx_frames == 0   -> Asterisk never sent audio: externalMedia or the bridge is wrong
    rx_frames >  0
      and peak == 0  -> audio arrived but is digital silence: wrong format, or not bridged
      and tx == rx   -> we echoed everything: the return path or the bridge is wrong
"""

from __future__ import annotations

import asyncio
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Dict, Optional


def new_session_uuid() -> str:
    return str(_uuid.uuid4())


@dataclass
class MediaSession:
    session_uuid: str
    # Populated as the call is assembled; any may be None if a step failed.
    call_channel_id: Optional[str] = None
    media_channel_id: Optional[str] = None
    bridge_id: Optional[str] = None
    label: str = ""
    # How this session drives the SEND direction:
    #   "echo" — write every received frame back (the real spike; a caller hears themselves)
    #   "tone" — emit a sine and ignore input, so a bridge recording proves the send path
    #            even with no human and nothing else making sound
    mode: str = "echo"
    # Bridge recording name. Two uses: the tone self-test's send-path evidence, and (agent
    # observability) the recording of a real agent conversation. It travels back to OWEN in
    # the session result, because owen-voice's bridge lives in ITS OWN Stasis app -- the
    # RecordingFinished event is delivered to this service and never to OWEN's consumer, so
    # OWEN cannot learn the recording exists any other way.
    recording_name: str | None = None
    # Per-session end-of-turn override. Exists for the self-test: its audio source is a
    # continuous recorded prompt whose gaps never reach the 700ms a human's pause does, so
    # the production threshold can never fire against it. Never set on a real call.
    vad_end_frames: int | None = None
    # Per-call TTS overrides, so voices can be compared on a REAL phone call rather than on
    # laptop speakers — the document's §3 warning is that 8kHz destroys much of what premium
    # TTS charges for, and the only honest test is down the actual phone.
    tts_voice: str | None = None
    tts_instructions: str | None = None
    tts_model: str | None = None

    created_at: float = field(default_factory=time.monotonic)
    connected_at: Optional[float] = None
    closed_at: Optional[float] = None

    rx_frames: int = 0
    rx_bytes: int = 0
    tx_frames: int = 0
    tx_bytes: int = 0
    # Peak absolute sample seen, 0..32767. Distinguishes "no audio" from "silent audio".
    peak_amplitude: int = 0
    dtmf: str = ""
    error: Optional[str] = None

    # --- conversation (step 2) ---
    turns: int = 0
    tool_calls: int = 0
    # Vendor-reported usage for the whole session (AI_AGENT_SPEC D14). OWEN turns this
    # into DERIVED cost rows -- never rated ones. Reported verbatim so a rate
    # correction can be re-applied later without re-running any calls.
    usage: dict = field(default_factory=dict)
    last_turn_ms: int = 0
    # What the CALLER experienced: silence between them stopping and hearing the first
    # syllable. `last_turn_ms` only records when synthesis finished, which nobody hears.
    last_first_audio_ms: int = 0
    # What the VAD actually sees. Without this, "the agent never answered" is a guess
    # between a wrong threshold, a silent stream and a stream that is never silent.
    rms_min: float = 1e9
    rms_max: float = 0.0
    rms_sum: float = 0.0
    rms_n: int = 0
    vad_starts: int = 0
    max_quiet_run: int = 0
    vad_ends: int = 0
    barge_suppressed: int = 0
    half_duplex_dropped: int = 0
    # Times playback ran dry mid-utterance. Should be 0; anything else is audible.
    underruns: int = 0
    # None = use the global default; set per call to A/B it.
    half_duplex: bool | None = None

    # --- flow-driven agent session (step 3) ---
    linkedid: str = ""
    caller_number: str = ""
    # The pinned agent-version config, as sent by OWEN. Empty for a standalone spike.
    agent: dict = field(default_factory=dict)
    # Caller context (CRM_CONTEXT_SPEC). `context` is OWEN's local half, already resolved;
    # `context_provider` is {url, headers, allowlist} for the external half, fetched here so
    # it overlaps media attach (C6). `context_blob` is what actually reached the model, and
    # `context_fields` the NAMES injected -- never the values (C4).
    context: dict = field(default_factory=dict)
    context_provider: dict = field(default_factory=dict)
    context_blob: str = ""
    context_fields: list = field(default_factory=list)
    context_degraded: bool = False
    # The port handed back to the flow interpreter, and any tool output.
    result_port: str | None = None
    result_data: dict = field(default_factory=dict)
    noise_utterances: int = 0
    # --- streaming STT (VOICE_STACK_MIGRATION M1/M2) ---
    # The configured streaming STT would not connect, so this call ran on the local turn
    # detector instead. Counted because the failure mode to fear is silent: every call still
    # works, 600ms slower, and nobody notices Flux has been down for a week.
    stt_degraded: bool = False
    # The stream died MID-call. Unlike the above this is not survivable -- the session ends
    # on the `failed` port and the flow routes to voicemail (M6).
    stt_failed: bool = False
    # Eager end-of-turn accounting: how often a draft was committed as-is, versus retracted
    # when the caller carried on. The ratio is what says whether eager mode is paying for
    # itself or just burning LLM calls.
    eager_hits: int = 0
    eager_retracted: int = 0
    # PER-TURN METRICS (agent observability). `last_*` above answers "how did the final turn
    # go", which is the least interesting turn on the call. Keeping every turn is what makes
    # "is this getting better or worse" answerable across a deploy, and it is what OWEN
    # persists onto the call so the question survives the container's log rotation.
    # One small dict per turn -- bounded by turns, not by frames.
    turn_metrics: list = field(default_factory=list)
    # Speaker-labelled, the shape the backend's `transcriptions.segments` already uses, so
    # persisting it in step 3 is a write rather than a translation.
    transcript: list = field(default_factory=list)

    # In-flight provider lookup, awaited (with a ceiling) just before the greeting.
    _context_task: object = field(default=None, repr=False)
    _writer: Optional[asyncio.StreamWriter] = field(default=None, repr=False)
    # Set when the conversation has ended, so POST /sessions can block on it.
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def connected(self) -> bool:
        return self.connected_at is not None and self.closed_at is None

    @property
    def duration_s(self) -> float:
        end = self.closed_at if self.closed_at is not None else time.monotonic()
        return round(end - self.created_at, 2)

    def agent_metrics(self) -> dict:
        """One row per call, summarising how the conversation actually performed.

        Percentiles rather than a mean: latency is what a caller FEELS, and a mean hides the
        one turn in five that took three seconds -- which is the turn they remember. p95 on a
        handful of turns is coarse, but it is the shape of the distribution that matters here,
        not a precise quantile.
        """
        def _p(key: str, p: float) -> int:
            vals = sorted(int(m.get(key, 0) or 0) for m in self.turn_metrics)
            if not vals:
                return 0
            i = min(len(vals) - 1, int(round((len(vals) - 1) * p)))
            return vals[i]

        firsts = sorted(int(m.get("first_audio_ms", 0) or 0) for m in self.turn_metrics)

        def pct(p: float) -> int:
            return _p("first_audio_ms", p)
        rms_avg = (self.rms_sum / self.rms_n) if self.rms_n else 0.0
        return {
            "turns": len(self.turn_metrics),
            "first_audio_ms_p50": pct(0.5),
            "first_audio_ms_p95": pct(0.95),
            "first_audio_ms_max": int(firsts[-1]) if firsts else 0,
            # WHERE the wait goes. first_audio is what the caller feels; these are its parts,
            # and they are what says which vendor to change. Medians, because one slow turn
            # should not redirect the next optimisation.
            "stt_ms_p50": _p("stt_ms", 0.5),
            "llm_ms_p50": _p("llm_ms", 0.5),
            "tts_ms_p50": _p("tts_ms", 0.5),
            # Should be 0. Anything else was audible as a gap inside a word, and is the
            # number that decides whether streaming playout was the right call.
            "underruns": self.underruns,
            "stt_degraded": self.stt_degraded,
            "stt_failed": self.stt_failed,
            "eager_hits": self.eager_hits,
            "eager_retracted": self.eager_retracted,
            # Caller-side audio. peak==0 with rx_frames>0 means we were bridged to silence;
            # peak at full scale means the caller's leg is clipping before it reaches us.
            "caller_peak": int(self.peak_amplitude),
            "caller_rms_avg": round(rms_avg, 1),
            "half_duplex_dropped": self.half_duplex_dropped,
            "barge_suppressed": self.barge_suppressed,
            "noise_utterances": self.noise_utterances,
            "recording_name": self.recording_name,
        }

    def snapshot(self) -> dict:
        """JSON-able state for the control API — the evidence that the transport worked."""
        return {
            "session_uuid": self.session_uuid,
            "label": self.label,
            "mode": self.mode,
            "call_channel_id": self.call_channel_id,
            "media_channel_id": self.media_channel_id,
            "bridge_id": self.bridge_id,
            "connected": self.connected,
            "duration_s": self.duration_s,
            "rx_frames": self.rx_frames,
            "rx_bytes": self.rx_bytes,
            "tx_frames": self.tx_frames,
            "tx_bytes": self.tx_bytes,
            "peak_amplitude": self.peak_amplitude,
            "dtmf": self.dtmf,
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "usage": self.usage,
            "context_fields": self.context_fields,
            "context_degraded": self.context_degraded,
            "rms_min": round(self.rms_min, 1) if self.rms_n else None,
            "rms_max": round(self.rms_max, 1) if self.rms_n else None,
            "rms_avg": round(self.rms_sum / self.rms_n, 1) if self.rms_n else None,
            "vad_starts": self.vad_starts,
            "max_quiet_run": self.max_quiet_run,
            "vad_ends": self.vad_ends,
            "barge_suppressed": self.barge_suppressed,
            "half_duplex_dropped": self.half_duplex_dropped,
            "underruns": self.underruns,
            "noise_utterances": self.noise_utterances,
            "stt_degraded": self.stt_degraded,
            "stt_failed": self.stt_failed,
            "eager_hits": self.eager_hits,
            "eager_retracted": self.eager_retracted,
            "turn_metrics": self.turn_metrics,
            "agent_metrics": self.agent_metrics(),
            "last_turn_ms": self.last_turn_ms,
            "last_first_audio_ms": self.last_first_audio_ms,
            "transcript": self.transcript,
            "error": self.error,
            "verdict": self.verdict(),
        }

    def verdict(self) -> str:
        """A one-line reading of the counters, so the result of the spike is unambiguous."""
        if self.error:
            return f"error: {self.error}"
        if self.connected_at is None:
            return "asterisk never connected — check externalMedia + advertise host/port"
        if self.mode == "agent":
            if self.rx_frames == 0:
                return "connected but no audio received — channel likely not bridged"
            if self.turns == 0:
                return "audio flowed but no complete turn — check VAD threshold / STT key"
            return f"OK — {self.turns} conversational turn(s)"
        if self.mode == "tone":
            # A tone session deliberately ignores input, so rx says nothing about success —
            # the proof is that we sent, and that Asterisk's bridge recording is non-silent.
            if self.tx_frames == 0:
                return "no tone frames sent — send path failed inside owen-voice"
            return (
                f"SENT {self.tx_frames} tone frames "
                "— confirm the bridge recording is non-silent to prove the return path"
            )
        if self.rx_frames == 0:
            return "connected but no audio received — channel likely not bridged"
        if self.peak_amplitude == 0:
            return "audio received but digital silence — check media format / bridge"
        if self.tx_frames == 0:
            return "audio received but nothing echoed back — write path failed"
        return "OK — audio flowed both ways"


def peak_of(pcm: bytes, current: int) -> int:
    """Highest absolute 16-bit LE sample in this frame, vs the running peak. Cheap enough for
    the hot path (one pass, no allocation beyond ints) and it is the single most useful signal
    for telling a dead transport apart from a silent one."""
    peak = current
    for i in range(0, len(pcm) - 1, 2):
        s = int.from_bytes(pcm[i:i + 2], "little", signed=True)
        a = -s if s < 0 else s
        if a > peak:
            peak = a
    return peak


class SessionRegistry:
    """UUID -> MediaSession. Small, in-memory, per-process — a session's lifetime is one call,
    and a restart drops the RTP anyway, so there is nothing worth persisting."""

    def __init__(self) -> None:
        self._sessions: Dict[str, MediaSession] = {}

    def create(self, label: str = "") -> MediaSession:
        s = MediaSession(session_uuid=new_session_uuid(), label=label)
        self._sessions[s.session_uuid] = s
        return s

    def get(self, session_uuid: str) -> Optional[MediaSession]:
        return self._sessions.get(session_uuid)

    def all(self) -> list[MediaSession]:
        return list(self._sessions.values())

    def active(self) -> list[MediaSession]:
        return [s for s in self._sessions.values() if s.connected]

    def drop(self, session_uuid: str) -> None:
        self._sessions.pop(session_uuid, None)

    def prune(self, keep_last: int = 20) -> None:
        """Keep the registry bounded: closed sessions are retained only so their counters can
        be read after the fact."""
        closed = sorted(
            (s for s in self._sessions.values() if s.closed_at is not None),
            key=lambda s: s.closed_at or 0,
        )
        for s in closed[:-keep_last] if len(closed) > keep_last else []:
            self._sessions.pop(s.session_uuid, None)


registry = SessionRegistry()
