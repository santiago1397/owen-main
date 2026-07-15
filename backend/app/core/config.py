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

    RECONCILE_WINDOW_HOURS: int = 4

    # Recordings
    RECORDINGS_DIR: str = "/data/recordings"
    RECORDING_RETENTION_DAYS: int = 30

    # Phase 6 — transcription + analysis (pluggable engines; dummy is the offline default).
    TRANSCRIPTION_ENGINE: str = "dummy"  # dummy | openai
    OPENAI_API_KEY: str = ""
    OPENAI_TRANSCRIBE_MODEL: str = "whisper-1"

    ANALYSIS_ENGINE: str = "dummy"  # dummy | claude | minimax
    ANTHROPIC_API_KEY: str = ""
    ANALYSIS_MODEL: str = "claude-haiku-4-5-20251001"

    # MiniMax (OpenAI-compatible chat completions with function-calling)
    MINIMAX_API_KEY: str = ""
    MINIMAX_BASE_URL: str = "https://api.minimax.io/v1"

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
