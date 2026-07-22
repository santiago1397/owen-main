import json
from dataclasses import dataclass
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class TwilioAccount:
    """One Twilio account's identity + credentials. Each account is treated as its own
    provider identity (its own `Provider` row / provider name), so calls, numbers and
    recordings attribute to the right account and downloads use the right token."""

    name: str  # provider name, e.g. "twilio" | "twilio-b"
    account_sid: str
    auth_token: str


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

    # Twilio credentials (per SERVER_SETUP.md: secrets live in .env.prod).
    # Single primary account; also the fallback when TWILIO_ACCOUNTS is unset.
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""

    # Additional Twilio accounts as a JSON list, each an object with keys
    # name/sid/token, e.g.
    #   TWILIO_ACCOUNTS=[{"name":"twilio","sid":"ACxxx","token":"t1"},
    #                    {"name":"twilio-b","sid":"ACyyy","token":"t2"}]
    # When set, it is the full list of Twilio accounts (include the primary explicitly).
    # When empty, we synthesize a single account named "twilio" from the fields above,
    # so existing single-account deployments keep working unchanged.
    TWILIO_ACCOUNTS: str = ""

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

    # GoHighLevel — completed-call relay. Same "Inbound Webhook" trigger pattern (plain
    # JSON, no auth), a *separate* URL so calls and texts can feed different GHL workflows.
    # Empty = call relay disabled. When a call reaches a terminal status we enqueue a relay
    # job; it waits GHL_CALL_RELAY_DELAY_SECONDS (so the recording→transcribe→analyze
    # pipeline can finish and the payload carries the AI analysis), re-deferring while a
    # recording exists but analysis is still pending, up to GHL_CALL_RELAY_MAX_WAIT_SECONDS.
    GHL_CALL_WEBHOOK_URL: str = ""
    GHL_CALL_RELAY_DELAY_SECONDS: int = 120
    GHL_CALL_RELAY_MAX_WAIT_SECONDS: int = 1800

    # GoHighLevel — inbound-email relay. Same "Inbound Webhook" trigger pattern (plain JSON,
    # no auth), a *separate* URL so parsed job emails can feed their own GHL workflow. Empty
    # = email relay disabled. Only *successfully parsed* emails are relayed (parse failures
    # are stored + flagged, never sent — see INBOUND_MAIL below).
    #   NOTE: superseded by the direct-API relay below when GHL_API_TOKEN is set. The webhook
    #   remains as a zero-code fallback for anyone who prefers GHL's Inbound-Webhook trigger.
    GHL_EMAIL_WEBHOOK_URL: str = ""

    # GoHighLevel — direct API relay (preferred: no premium per-execution charge, and we get
    # retries/dedup/logging). When GHL_API_TOKEN + GHL_LOCATION_ID are set, a parsed job email
    # upserts a Contact and creates an Opportunity in the configured pipeline via the v2 API,
    # instead of POSTing the webhook. Auth is a sub-account Private Integration Token (PIT).
    #   Pipeline/stage IDs are resolved once and pasted here (a one-off lookup lists them).
    GHL_API_TOKEN: str = ""                 # Private Integration Token (sub-account scope)
    GHL_LOCATION_ID: str = ""               # sub-account / location id
    GHL_PIPELINE_ID: str = ""               # target pipeline (e.g. "Dream Team Roofing AHS")
    GHL_PIPELINE_STAGE_ID: str = ""         # stage new jobs land in (blank = first stage of the pipeline)
    GHL_API_BASE: str = "https://services.leadconnectorhq.com"
    GHL_API_VERSION: str = "2021-07-28"     # required Version header for v2 endpoints

    @property
    def ghl_api_enabled(self) -> bool:
        return bool(self.GHL_API_TOKEN and self.GHL_LOCATION_ID)

    # Inbound email ingestion (Hostinger IMAP). The worker polls this mailbox for new mail
    # from INBOUND_MAIL_SENDER, parses templated job-notification emails, stores them, and
    # relays parsed ones to GHL. Empty INBOUND_MAIL_HOST/USER = poller disabled (no-op).
    # We authenticate with the mailbox password directly (Hostinger has no OAuth/app-passwords)
    # over TLS, and only ever fetch mail matching the sender filter — other mail is untouched.
    INBOUND_MAIL_HOST: str = ""            # e.g. imap.hostinger.com
    INBOUND_MAIL_PORT: int = 993           # IMAP over SSL/TLS
    INBOUND_MAIL_USER: str = ""            # full mailbox address = IMAP username
    INBOUND_MAIL_PASSWORD: str = ""
    INBOUND_MAIL_FOLDER: str = "INBOX"
    # Only mail from this address is fetched/parsed/marked-seen. Scoped to Dispatch for now.
    INBOUND_MAIL_SENDER: str = "notifications@dispatch.me"
    # Poll cadence. 90s meets the "within a minute or two" latency bar and is well within
    # Hostinger's IMAP limits (one short-lived connection per poll).
    INBOUND_MAIL_POLL_SECONDS: int = 90
    # Max messages pulled per poll (backlog is drained across successive polls).
    INBOUND_MAIL_BATCH: int = 25
    # Mark handled messages \Seen so they aren't re-fetched next poll. DB dedupe on the
    # RFC Message-ID is the real idempotency guard; this is only a fetch-efficiency measure.
    INBOUND_MAIL_MARK_SEEN: bool = True

    # --- Asterisk / BulkVS telephony (additive, DARK + flag-gated) --------------------
    # Master switch for the native-Asterisk + BulkVS platform. OFF by default: with it
    # off nothing about the existing app changes — no telephony consumers start and the
    # Twilio/SignalWire/GHL paths are entirely untouched. Flip on only once the native
    # Asterisk host is provisioned (see asterisk/README.md). /health/telephony is the
    # non-gating probe that confirms it came alive.
    ASTERISK_ENABLED: bool = False

    # ARI (Asterisk REST Interface). The backend reaches Asterisk running natively on the
    # host via the docker host-gateway; ARI is bound to loopback + the gateway and
    # firewalled to the callmon-net subnet (never public — same pattern as Postgres).
    # Creds come from env, never hardcoded into the asterisk/ config templates.
    ARI_HOST: str = "host.docker.internal"
    ARI_PORT: int = 8088
    ARI_USERNAME: str = ""
    ARI_PASSWORD: str = ""
    ARI_APP: str = "owen"  # Stasis application name the dialplan hands calls to

    # BulkVS SIP trunk. Secrets from env. Inbound auth is by SBC source IP (see
    # asterisk/README.md), so the trunk name identifies the PJSIP endpoint/aor/identify
    # rendered from the asterisk/ templates; username/password cover outbound REGISTER/auth.
    BULKVS_TRUNK_NAME: str = "bulkvs"
    BULKVS_SIP_USERNAME: str = ""
    BULKVS_SIP_PASSWORD: str = ""
    BULKVS_FROM_NUMBER: str = ""  # default outbound caller-ID (E.164)

    @property
    def ari_base_url(self) -> str:
        return f"http://{self.ARI_HOST}:{self.ARI_PORT}"

    @property
    def ari_ws_url(self) -> str:
        """ARI events WebSocket, subscribed to our Stasis app. Creds ride in the query
        string (api_key=user:pass) since ARI's WS accepts no auth header — safe because
        the endpoint is loopback/gateway-only and firewalled (never public)."""
        return (
            f"ws://{self.ARI_HOST}:{self.ARI_PORT}/ari/events"
            f"?app={self.ARI_APP}&api_key={self.ARI_USERNAME}:{self.ARI_PASSWORD}"
        )

    def twilio_accounts(self) -> list[TwilioAccount]:
        """All configured Twilio accounts. Parses TWILIO_ACCOUNTS when set, otherwise
        falls back to the legacy single-account globals (named "twilio"). Entries with a
        blank sid/token are dropped so callers can rely on credentials being present."""
        raw = self.TWILIO_ACCOUNTS.strip()
        if raw:
            entries = json.loads(raw)
            accounts = [
                TwilioAccount(
                    name=str(e["name"]).strip(),
                    account_sid=str(e.get("sid", "")).strip(),
                    auth_token=str(e.get("token", "")).strip(),
                )
                for e in entries
            ]
        else:
            accounts = [
                TwilioAccount(
                    name="twilio",
                    account_sid=self.TWILIO_ACCOUNT_SID,
                    auth_token=self.TWILIO_AUTH_TOKEN,
                )
            ]
        return [a for a in accounts if a.name and a.account_sid and a.auth_token]

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
