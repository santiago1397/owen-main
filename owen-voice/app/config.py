"""owen-voice settings. Env-driven, no OWEN imports.

Deliberately standalone (AI_AGENT_SPEC D2, "separable-but-co-located"): this service must
carry no `localhost` assumptions and no OWEN module dependency, so relocating it to another
host later is a config change rather than a refactor. The August 2026 VPS migration cost
three hardcoded `172.19.0.1` occurrences — nothing here gets hardcoded.
"""

from __future__ import annotations

import os


def _s(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _i(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


class Settings:
    # --- ARI (Asterisk runs NATIVELY on the host; we reach it over the host gateway) -------
    ARI_HOST: str = _s("ARI_HOST", "host.docker.internal")
    ARI_PORT: int = _i("ARI_PORT", 8088)
    ARI_USERNAME: str = _s("ARI_USERNAME", "owen")
    ARI_PASSWORD: str = _s("ARI_PASSWORD")
    # Our OWN Stasis app, deliberately NOT OWEN's `ARI_APP`. Two consumers on one Stasis app
    # fight over the same events; a separate app means this service can be started, crashed
    # and restarted with zero effect on live call handling.
    ARI_APP: str = _s("VOICE_ARI_APP", "owen-voice")

    # --- AudioSocket listener --------------------------------------------------------------
    # We LISTEN; Asterisk dials in (ARI externalMedia `connection_type=client`).
    AUDIOSOCKET_BIND: str = _s("VOICE_AUDIOSOCKET_BIND", "0.0.0.0")
    AUDIOSOCKET_PORT: int = _i("VOICE_AUDIOSOCKET_PORT", 9092)
    # What we tell Asterisk to connect BACK to. Asterisk is on the host and the compose file
    # publishes our port on loopback only, so the host reaches us at 127.0.0.1 — no docker
    # bridge IP anywhere, which is exactly the class of value that broke during the migration.
    AUDIOSOCKET_ADVERTISE: str = _s("VOICE_AUDIOSOCKET_ADVERTISE", "127.0.0.1:9092")

    # --- Control API -----------------------------------------------------------------------
    HTTP_PORT: int = _i("VOICE_HTTP_PORT", 8099)

    # --- Media -----------------------------------------------------------------------------
    # `slin` is 8 kHz signed linear — what AudioSocket carries and what telephony runs at.
    MEDIA_FORMAT: str = _s("VOICE_MEDIA_FORMAT", "slin")

    # --- Cascaded pipeline: STT / LLM / TTS (step 2, AI_AGENT_SPEC D11) ---------------------
    # Shipped against keys that already exist in .env.prod. The spec's preferred stack
    # (Deepgram Flux + Aura-2) slots in as extra classes in providers.py, changing nothing else.
    OPENAI_API_KEY: str = _s("OPENAI_API_KEY")
    OPENAI_BASE_URL: str = _s("OPENAI_BASE_URL", "https://api.openai.com/v1")

    STT_PROVIDER: str = _s("VOICE_STT_PROVIDER", "openai")
    # gpt-4o-mini-transcribe measured at 452ms against whisper-1's 1415ms on this host for a
    # 3s utterance — the same API, the same key, 3x faster. STT was the largest single term in
    # the turn budget, so this is the cheapest latency win available.
    STT_MODEL: str = _s("VOICE_STT_MODEL", "gpt-4o-mini-transcribe")
    STT_LANGUAGE: str = _s("VOICE_STT_LANGUAGE", "en")

    # Any OpenAI-compatible endpoint: OpenAI, MiniMax, DeepSeek, Kimi, or an aggregator.
    # Defaults to OpenAI because that key is already present; point LLM_BASE_URL at MiniMax
    # (https://api.minimax.io/v1) or a Western host for DeepSeek/Kimi to swap the brain.
    LLM_PROVIDER: str = _s("VOICE_LLM_PROVIDER", "openai_compatible")
    LLM_BASE_URL: str = _s("VOICE_LLM_BASE_URL", "https://api.openai.com/v1")
    LLM_API_KEY: str = _s("VOICE_LLM_API_KEY") or _s("OPENAI_API_KEY")
    LLM_MODEL: str = _s("VOICE_LLM_MODEL", "gpt-4o-mini")
    LLM_TEMPERATURE: float = float(_s("VOICE_LLM_TEMPERATURE", "0.6") or 0.6)
    # Hard cap: this is speech. Long replies make the caller sit through all of it.
    LLM_MAX_TOKENS: int = _i("VOICE_LLM_MAX_TOKENS", 160)
    # Rolling window of turns kept in context — the spec's cost warning is about exactly this.
    LLM_HISTORY_TURNS: int = _i("VOICE_LLM_HISTORY_TURNS", 8)

    TTS_PROVIDER: str = _s("VOICE_TTS_PROVIDER", "openai")
    TTS_MODEL: str = _s("VOICE_TTS_MODEL", "gpt-4o-mini-tts")
    TTS_VOICE: str = _s("VOICE_TTS_VOICE", "alloy")
    TTS_MAX_CHARS: int = _i("VOICE_TTS_MAX_CHARS", 1200)
    # gpt-4o-mini-tts accepts plain-English direction for DELIVERY (pace, warmth, tone) —
    # a large, free improvement most deployments never use. Ignored by older tts-1 models.
    # Delivery direction for the gpt-4o-mini-tts family. DEFAULT OFF: strong style prompts
    # ("warm", "unhurried", "never sound like a recording") make the model perform, and
    # performance -- breathiness, uneven pacing, wide dynamics -- is exactly what an 8 kHz
    # phone codec destroys, so it lands as artefacts rather than warmth. Opt in per call and
    # judge it down a real phone, never on laptop speakers.
    TTS_INSTRUCTIONS: str = _s("VOICE_TTS_INSTRUCTIONS", "")

    # --- Turn detection ---------------------------------------------------------------------
    # Ours to do because we are not on Deepgram Flux, which has end-of-turn built in.
    VAD_SPEECH_RMS: float = float(_s("VOICE_VAD_SPEECH_RMS", "700") or 700)
    # x20ms. 600ms rather than 700: this silence is pure perceived latency on EVERY turn and
    # is not counted in turn timings (the clock starts when the turn ends). Short enough to
    # feel responsive, long enough to survive a mid-sentence pause.
    VAD_END_FRAMES: int = _i("VOICE_VAD_END_FRAMES", 30)
    # While the agent is SPEAKING, require this much more energy before believing the caller
    # has interrupted. Without it the agent barges in on ITSELF -- its own voice returning via
    # a speakerphone (or line artefacts) trips the detector, the reply is cancelled, the next
    # one is cancelled the same way, and the caller hears nothing but fragments. Observed live.
    VAD_BARGE_SCALE: float = float(_s("VOICE_VAD_BARGE_SCALE", "3.0") or 3.0)
    # Never allow an interruption in the first moments of a reply: that is exactly when the
    # agent's own opening syllables would come back through a speakerphone.
    BARGE_GUARD_MS: int = _i("VOICE_BARGE_GUARD_MS", 900)
    # An utterance quieter than this is noise, not speech. Whisper-family models hallucinate
    # confidently on near-silence -- a live call produced "لا لا لا لا" and "Привет" from a
    # quiet line -- and each hallucination costs an STT call and a wrong turn.
    VAD_MIN_UTTERANCE_RMS: float = float(_s("VOICE_VAD_MIN_UTTERANCE_RMS", "300") or 300)

    # --- Agent (step 2 uses one hardcoded agent; step 3 reads these from agent_versions) -----
    AGENT_SYSTEM_PROMPT: str = _s(
        "VOICE_AGENT_SYSTEM_PROMPT",
        "You are a friendly receptionist for a Florida roofing company. Keep every reply "
        "under two short sentences, because it is spoken aloud on a phone call. Ask one "
        "question at a time. Collect the caller's name, service address and what is wrong "
        "with their roof. If they ask for a human, tell them you will pass them along.",
    )
    AGENT_GREETING: str = _s(
        "VOICE_AGENT_GREETING",
        "Hi, thanks for calling. You're speaking with an AI assistant. How can I help?",
    )
    AGENT_MAX_CALL_SECONDS: int = _i("VOICE_AGENT_MAX_CALL_SECONDS", 300)
    AGENT_MAX_SILENCE_SECONDS: int = _i("VOICE_AGENT_MAX_SILENCE_SECONDS", 30)

    # --- Spike controls ---------------------------------------------------------------------
    # Trunk to place the self-test call over, and the DID to present. Both only used by
    # POST /spike/call, which exists to prove the transport against your own phone.
    TRUNK_NAME: str = _s("BULKVS_TRUNK_NAME", "bulkvs")
    FROM_NUMBER: str = _s("BULKVS_FROM_NUMBER")
    # Hard ceiling on any spike call, so a forgotten test cannot hold a trunk channel.
    MAX_CALL_SECONDS: int = _i("VOICE_MAX_CALL_SECONDS", 120)

    @property
    def ari_base(self) -> str:
        return f"http://{self.ARI_HOST}:{self.ARI_PORT}/ari"

    @property
    def ari_ws_url(self) -> str:
        return (
            f"ws://{self.ARI_HOST}:{self.ARI_PORT}/ari/events"
            f"?api_key={self.ARI_USERNAME}:{self.ARI_PASSWORD}&app={self.ARI_APP}"
        )


settings = Settings()
