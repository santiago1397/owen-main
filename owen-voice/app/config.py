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
