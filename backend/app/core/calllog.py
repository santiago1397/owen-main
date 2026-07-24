"""Call-lifecycle logging: one consistent, greppable, correlated line per important step.

The platform's calls run across several async paths (the ARI WS consumer, the flow
interpreter, the default-handler, the operator control endpoints, the low-level ARI client).
To debug "what happened on THIS call" you want to `grep linkedid=<x>` and read the whole story
in order. `clog()` enforces one format for that:

    call.<phase> linkedid=<lid> channel=<id> key=value key=value

Everything is optional except the phase; None-valued fields are dropped so lines stay tight.
This is logging ONLY — it never raises and never changes behavior. Values are coerced to a
compact string; secrets must not be passed in (callers pass ids/numbers/counts, not creds).

`setup_logging()` centralizes the root logger config (format with timestamps + LOG_LEVEL) so
both the API and the worker emit the same, timestamped, level-labelled lines.
"""

from __future__ import annotations

import logging

from app.core.config import settings

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"

_configured = False


def setup_logging() -> None:
    """Configure the root logger once (idempotent). Format carries a timestamp + level + logger
    name so call-trace lines are orderable and attributable; level comes from LOG_LEVEL."""
    global _configured
    level = _resolve_level(settings.LOG_LEVEL)
    # force=True so we win over any earlier basicConfig (import order can pre-configure root).
    logging.basicConfig(level=level, format=_FORMAT, datefmt=_DATEFMT, force=True)
    # httpx logs every request at INFO ("HTTP Request: POST ... 200 OK"); that's redundant with
    # our own ARI request logging and doubles the noise, so cap it at WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _configured = True


def _resolve_level(value: str | int) -> int:
    if isinstance(value, int):
        return value
    name = str(value or "INFO").strip().upper()
    resolved = logging.getLevelName(name)
    return resolved if isinstance(resolved, int) else logging.INFO


def _fmt_fields(fields: dict) -> str:
    parts = []
    for k, v in fields.items():
        if v is None:
            continue
        s = str(v)
        # Keep each token whitespace-free so `key=value` pairs stay greppable.
        if " " in s:
            s = s.replace(" ", "_")
        parts.append(f"{k}={s}")
    return " ".join(parts)


def clog(
    logger: logging.Logger,
    phase: str,
    *,
    linkedid: str | None = None,
    channel: str | None = None,
    level: int = logging.INFO,
    **fields,
) -> None:
    """Emit one `call.<phase> linkedid=… channel=… k=v` line. Best-effort; never raises."""
    try:
        head = f"call.{phase}"
        tail = _fmt_fields({"linkedid": linkedid, "channel": channel, **fields})
        logger.log(level, "%s %s" % (head, tail) if tail else head)
    except Exception:  # noqa: BLE001 - logging must never break a call
        pass
