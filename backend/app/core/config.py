from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven config. Values come from .env.prod on the server."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ENVIRONMENT: str = "development"
    DEBUG: bool = True

    # Comma-separated origins allowed to call the API (the frontend subdomain in prod).
    CORS_ORIGINS: str = ""

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    # Database (native host Postgres, reached via host.docker.internal in prod)
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "callmon"
    POSTGRES_PASSWORD: str = "callmon"
    POSTGRES_DB: str = "callmon"

    # Auth
    SECRET_KEY: str = "change-me"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14
    JWT_ALGORITHM: str = "HS256"

    # Business timezone for all daily/weekly/monthly bucketing
    BUSINESS_TZ: str = "America/New_York"

    # Twilio credentials (per SERVER_SETUP.md: secrets live in .env.prod)
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""

    # SignalWire (Compatibility/cXML API)
    SIGNALWIRE_PROJECT_ID: str = ""
    SIGNALWIRE_AUTH_TOKEN: str = ""
    SIGNALWIRE_SPACE_URL: str = ""  # e.g. yourspace.signalwire.com

    # Shared secret (HTTP Basic Auth) for Call Flow Builder's generic "Request" node,
    # which can't produce a Twilio-style HMAC signature. Only accepted as an alternate
    # verification path on /webhooks/signalwire/recording, never in place of the
    # signature check for real signed webhooks.
    SIGNALWIRE_CFB_WEBHOOK_SECRET: str = ""

    RECONCILE_WINDOW_HOURS: int = 4

    # Recordings
    RECORDINGS_DIR: str = "/data/recordings"
    RECORDING_RETENTION_DAYS: int = 30
    # Delete the provider's copy of the recording right after we download it, so
    # provider-side storage is never billed (the local copy + transcript remain).
    DELETE_REMOTE_RECORDING: bool = True

    # Phase 6 — transcription + analysis (pluggable engines; dummy is the offline default).
    TRANSCRIPTION_ENGINE: str = "dummy"  # dummy | openai
    OPENAI_API_KEY: str = ""
    OPENAI_TRANSCRIBE_MODEL: str = "whisper-1"

    # Dual-channel ("who said what") transcription. SignalWire's Start Call Recording
    # node records in stereo: each call leg lands on its own channel, so the channel IS
    # the speaker — no AI diarization guessing. When enabled, the transcribe handler
    # splits a 2-channel recording into two mono tracks, transcribes each, and merges
    # them into a speaker-labeled transcript. Mono recordings are unaffected.
    #   Kill-switch: set false to force the single-transcript path regardless of channels.
    STEREO_TRANSCRIPTION_ENABLED: bool = True
    #   Which channel index (0 or 1) carries the inbound caller; the other is the operator.
    #   MUST be confirmed against a real stereo test call before trusting labels (the
    #   node doesn't document a guaranteed mapping). One env flip if SignalWire changes it.
    STEREO_CALLER_CHANNEL: int = 0
    #   Segment timestamps (needed to interleave the two legs) require whisper-1 — the prod
    #   OPENAI_TRANSCRIBE_MODEL (gpt-4o-transcribe) can't return them. whisper-1 hallucinates
    #   on the silent stretches of a split channel, so segments are filtered by the two
    #   thresholds below (whisper-1's per-segment no_speech_prob / avg_logprob).
    OPENAI_STEREO_TRANSCRIBE_MODEL: str = "whisper-1"
    STEREO_MAX_NO_SPEECH_PROB: float = 0.6   # drop segment if no_speech_prob above this
    STEREO_MIN_AVG_LOGPROB: float = -1.2     # drop segment if avg_logprob below this

    ANALYSIS_ENGINE: str = "dummy"  # dummy | claude | minimax
    ANTHROPIC_API_KEY: str = ""
    ANALYSIS_MODEL: str = "claude-haiku-4-5-20251001"

    # MiniMax (OpenAI-compatible chat completions with function-calling)
    MINIMAX_API_KEY: str = ""
    MINIMAX_BASE_URL: str = "https://api.minimax.io/v1"

    # GoHighLevel — inbound SMS relay. We only ever POST inbound texts to a GHL Workflow
    # "Inbound Webhook" trigger URL (plain JSON, no auth/OAuth). Empty = relay disabled.
    GHL_INBOUND_WEBHOOK_URL: str = ""

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
