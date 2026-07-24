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

    # Root log level (name or number: DEBUG/INFO/WARNING/...). DEBUG additionally surfaces every
    # ARI HTTP request line so a call can be traced end-to-end from the worker logs; INFO keeps
    # the semantic per-phase `call.*` lines. See app/core/calllog.py.
    LOG_LEVEL: str = "INFO"

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
    # The refresh token is what keeps an installed phone app signed in. It is ROTATED on
    # every /api/auth/refresh, so this is a sliding window: anyone opening OWEN at least
    # once in this many days never sees the login screen again. Sized for an internal
    # tool used from a phone; shorten it if OWEN is ever opened to outside users.
    REFRESH_TOKEN_EXPIRE_DAYS: int = 60
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

    # Voice AI agents (Ticket 11) — pluggable VoiceAgentSession engines, mirroring the
    # transcription seam. Per-agent `engine` selects the runtime; this GLOBAL kill-switch,
    # when set to a non-empty engine name, FORCES every agent onto that engine regardless of
    # its per-agent setting (e.g. flip to "dummy" to instantly stop all real audio sessions).
    # Empty (default) = honour each agent's own `engine`. `dummy` is the offline default so
    # the node + interpreter + version-pinning are testable without real audio; the real
    # `openai_realtime` runtime is a later ticket (12) and is a registered-but-stubbed engine.
    VOICE_AGENT_ENGINE: str = ""  # "" = per-agent | dummy | openai_realtime | vapi | diy

    # OpenAI Realtime voice-agent runtime (Ticket 12). Only active when an agent's engine (or
    # the kill-switch above) selects "openai_realtime"; `dummy` stays the offline default. Reuses
    # OPENAI_API_KEY (declared above for transcription). The per-agent `model` overrides this.
    OPENAI_REALTIME_MODEL: str = "gpt-4o-realtime-preview"
    OPENAI_REALTIME_VOICE: str = "alloy"  # default TTS voice when the agent config sets none
    # WS-reconnect retries before the session gives up and returns the `failed` port (→ the
    # flow's default_fallback/voicemail). Design decision is exactly ONE retry.
    VOICE_AGENT_WS_RECONNECTS: int = 1

    # Flow-prompt TTS (Ticket 15.2). Flow graphs store prompts as plain text; the backend
    # synthesizes them with OpenAI TTS (reusing OPENAI_API_KEY above) into 8kHz-mono WAVs
    # cached under <RECORDINGS_DIR>/tts/ (content-addressed by sha256(text|voice)), which
    # Asterisk plays via absolute-path sound: URIs (see asterisk/README.md for the shared
    # host path requirement). Synthesized at flow activation (best-effort prewarm) and
    # lazily at call time on a cache miss; a TTS failure skips playback, never dead-airs.
    TTS_MODEL: str = "tts-1"
    TTS_VOICE: str = "alloy"

    # Flow safety net (Ticket 15.6). When a flow-ASSIGNED number's flow fails to resolve
    # (deleted / no active version) or the interpreter crashes at the top of the call, the
    # caller is blind-forwarded (answer + dial + bridge) to this number instead of dead
    # air. E.164. Empty = no forward (the call is hung up cleanly instead).
    FLOW_FALLBACK_FORWARD_NUMBER: str = ""

    # --- Default call handling for UNASSIGNED Asterisk numbers (Ticket 18) ----------------
    # When a BulkVS/Asterisk DID has NO flow assigned, OWEN no longer no-ops the call (which
    # left dead air). The built-in default: answer -> consent notice -> ring every AVAILABLE
    # operator's softphone at once (first to answer is bridged, the rest stop ringing) -> if
    # nobody is available or nobody answers in time, take a voicemail. A real assigned flow
    # OVERRIDES this default. See docs/DEFAULT_CALL_HANDLING_SPEC.md.
    #   Master switch for the ring-operators step. False = go straight to voicemail for
    #   unassigned numbers (still never dead air). Gated by ASTERISK_ENABLED regardless.
    NO_FLOW_RING_OPERATORS: bool = True
    #   How long (seconds) all available operators ring before the call rolls to voicemail.
    OPERATOR_RING_TIMEOUT_SECONDS: int = 25
    #   Recording-consent notice played to the caller BEFORE operators ring (FL all-party
    #   consent — ARCHITECTURE.md #17). Plain text is TTS-synthesized like a flow prompt; a
    #   "sound:" URI is played as-is. Empty = no notice (skip straight to the ring).
    INBOUND_CONSENT_MEDIA: str = "This call may be recorded for quality and training purposes."
    #   Record operator-answered inbound calls (feeds transcription/analysis/GHL). Off = the
    #   bridge is not recorded (no consent concern, but no transcript either).
    INBOUND_RECORDING_ENABLED: bool = True

    # Voicemail (used by the unassigned-number default AND the flow `voicemail` node). The
    # greeting is played (plain text -> TTS; "sound:" URI as-is), then a beep, then the caller
    # is recorded until they hang up or fall silent, capped at the max duration. The WAV rides
    # the existing recording->transcribe->analyze->GHL pipeline (name prefixed with the call
    # Linkedid) and surfaces in the Inbox thread + Calls.
    VOICEMAIL_GREETING: str = (
        "You've reached us, but we can't take your call right now. "
        "Please leave a message after the tone and we'll get back to you as soon as we can."
    )
    VOICEMAIL_MAX_DURATION_SECONDS: int = 120
    VOICEMAIL_MAX_SILENCE_SECONDS: int = 5

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

    # Where Asterisk writes flow-recorded WAVs on the HOST, bind-mounted into the app +
    # worker containers (read-only is fine) so the recording "fetch" is a local file move
    # rather than an HTTP download. Asterisk's res_stasis writes ARI recordings under its
    # spool `recording/` subdir; mount that dir here. See asterisk/README.md.
    #   host  /var/spool/asterisk/recording  ->  container  /data/asterisk-spool  (ro)
    ASTERISK_SPOOL_DIR: str = "/data/asterisk-spool"

    # Window (hours) the Asterisk CDR reconciler scans the cdr table for; and its cadence.
    ASTERISK_CDR_WINDOW_HOURS: int = 4
    ASTERISK_CDR_POLL_SECONDS: int = 300

    # BulkVS SIP trunk. Secrets from env. Inbound auth is by SBC source IP (see
    # asterisk/README.md), so the trunk name identifies the PJSIP endpoint/aor/identify
    # rendered from the asterisk/ templates; username/password cover outbound REGISTER/auth.
    BULKVS_TRUNK_NAME: str = "bulkvs"
    BULKVS_SIP_USERNAME: str = ""
    BULKVS_SIP_PASSWORD: str = ""
    BULKVS_FROM_NUMBER: str = ""  # default outbound caller-ID (E.164)

    # BulkVS REST API (number inventory sync — SEPARATE from the SIP trunk creds above).
    # There is no inventory webhook, so DID inventory is POLLED from GET /tnRecord (HTTP
    # Basic auth). /tnRecord lists owned TNs and carries `ReferenceID` (the operator's
    # user-note/label) which we one-way mirror into Number.friendly_name. Buying/releasing
    # DIDs happens in the BulkVS portal — OWEN only MIRRORS inventory (add-only + soft-
    # release + reactivate). Empty creds OR ASTERISK_ENABLED off => the sync never runs.
    BULKVS_API_BASE: str = "https://portal.bulkvs.com/api/v1.0"
    BULKVS_API_USERNAME: str = ""
    BULKVS_API_PASSWORD: str = ""
    # Provider identity stamped onto synced DIDs (owner = carrier, media = who carries audio).
    BULKVS_OWNER_PROVIDER: str = "bulkvs"
    BULKVS_MEDIA_PROVIDER: str = "asterisk"
    # Poll cadence for the inventory sync (well within a portal-mirrored latency need).
    BULKVS_SYNC_POLL_SECONDS: int = 300

    # --- OpenPhone (READ-ONLY) — see docs/GHL_SYNC_SPEC.md D11 + D16 ---------------------
    # OpenPhone is the account the team makes OUTBOUND customer calls from. OWEN reads those
    # call logs to record follow-up TOUCHES on existing leads (never as leads themselves).
    #
    # HARD CONSTRAINT (owner-mandated): OWEN performs GET requests ONLY against OpenPhone.
    # It never sends a message, never places a call, never writes a contact — anything that
    # could incur a charge. This is enforced structurally: app/providers/openphone_client.py
    # implements no write methods at all. Do not add one.
    #
    # Empty key => the integration is a no-op (nothing polls, nothing is read).
    OPENPHONE_API_KEY: str = ""
    OPENPHONE_API_BASE: str = "https://api.openphone.com/v1"

    @property
    def openphone_enabled(self) -> bool:
        return bool(self.OPENPHONE_API_KEY)

    # --- Operator WebRTC softphone (Ticket 13, additive, gated on ASTERISK_ENABLED) -------
    # The operator answers platform calls in the browser via a per-operator chan_pjsip
    # WebRTC endpoint (SIP.js, wss + DTLS-SRTP). Signalling wss is fronted by Traefik; media
    # reuses the existing 10000-10200/udp RTP range; coturn (TLS/443) relays through firewalls
    # with backend-minted ephemeral creds. See asterisk/README.md + app/telephony/.
    #   The digest password for the static per-operator pjsip WebRTC endpoints (rendered into
    #   pjsip.conf from ${OPERATOR_SIP_SECRET}). Returned only to authenticated operators by
    #   the cred-minting endpoint; the real gate is app login (ARI stays server-side).
    OPERATOR_SIP_SECRET: str = ""
    # SIP domain / realm the softphone registers against (usually the app/public host).
    OPERATOR_SIP_DOMAIN: str = ""
    # Public wss URL of the Traefik-fronted Asterisk WebSocket, e.g. wss://api.<APP_DOMAIN>/ws.
    OPERATOR_WSS_URL: str = ""
    # How long a minted SIP credential is advertised as valid before the frontend re-mints.
    OPERATOR_SIP_TTL_SECONDS: int = 3600
    # coturn shared secret (use-auth-secret / TURN REST API). Empty => TURN disabled (no
    # ice_servers minted; STUN/host candidates only). Never committed — lives in .env.prod.
    TURN_STATIC_SECRET: str = ""
    # coturn ICE server URLs advertised to SIP.js, comma-separated, e.g.
    #   turns:turn.<APP_DOMAIN>:443?transport=tcp,stun:turn.<APP_DOMAIN>:443
    TURN_URLS: str = ""
    # Ephemeral TURN credential lifetime (short — coturn validates the embedded expiry).
    TURN_TTL_SECONDS: int = 3600

    @property
    def turn_urls(self) -> list[str]:
        return [u.strip() for u in self.TURN_URLS.split(",") if u.strip()]

    # --- Manual operator OUTBOUND calling (Ticket 14, additive, gated on ASTERISK_ENABLED) ---
    # ARI media URI played to the CALLEE before the operator is bridged in — the outbound
    # analogue of the inbound flow's entry recording-consent notice (recording is on by
    # default for outbound calls). A "sound:" prompt provisioned on the Asterisk host.
    OUTBOUND_CONSENT_MEDIA: str = "sound:owen/outbound-recording-consent"
    # Recording ON by default for manual outbound calls; flip off only to disable it globally.
    OUTBOUND_RECORDING_ENABLED: bool = True

    @property
    def bulkvs_api_enabled(self) -> bool:
        """The BulkVS inventory sync only runs when the platform flag is on AND REST creds
        are configured — keeps the platform dark by default (nothing new runs)."""
        return bool(
            self.ASTERISK_ENABLED
            and self.BULKVS_API_USERNAME
            and self.BULKVS_API_PASSWORD
        )

    @property
    def ari_base_url(self) -> str:
        return f"http://{self.ARI_HOST}:{self.ARI_PORT}"

    @property
    def ari_ws_url(self) -> str:
        """ARI events WebSocket, subscribed to our Stasis app. Creds ride in the query
        string (api_key=user:pass) since ARI's WS accepts no auth header — safe because
        the endpoint is loopback/gateway-only and firewalled (never public).

        `subscribeAll=true` is REQUIRED for terminal call status. Without it an app only
        receives events for channels currently IN Stasis: hanging up sends StasisEnd (which
        is deliberately non-terminal — the channel may still be live) and the implicit
        subscription ends, so the ChannelDestroyed that actually carries the Q.850 hangup
        cause is never delivered. Every call then sat at "in-progress" forever. Verified
        against a live Asterisk: a second WS with subscribeAll saw ChannelDestroyed for the
        same entry channel the app's own WS never received.

        The extra global events are cheap and safe: AsteriskEventRouter.route drops anything
        that doesn't map to a status, isn't the entry channel (id == Linkedid), or is a
        flow-dial leg, and dedups on "{Linkedid}:{status}".
        """
        return (
            f"ws://{self.ARI_HOST}:{self.ARI_PORT}/ari/events"
            f"?app={self.ARI_APP}&subscribeAll=true"
            f"&api_key={self.ARI_USERNAME}:{self.ARI_PASSWORD}"
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
