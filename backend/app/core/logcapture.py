"""Mirror WARNING+ log records into Postgres so `/api/ai/errors` can see them.

Docker's json-file logs are still the real log stream. They are also unreachable over HTTP:
they live on the VPS behind SSH, they rotate away at 10 MB, and the `app` container has no way
to read the `worker` container's. Since both containers already talk to the same database,
mirroring the serious records there is the cheapest thing that makes errors visible to an API
caller — and it survives restarts, which rotated logs do not.

Three properties this must have, in order of importance:

1. **It must never break the process it observes.** Every failure path is swallowed. If the
   database is down — exactly when errors are most interesting — logging still works normally
   and we simply lose the mirror.
2. **It must never block.** The handler only appends to a bounded in-memory queue and returns.
   A daemon thread does the I/O. When the queue is full, records are DROPPED rather than
   allowed to slow down a call, and the drop is counted so the gap is visible rather than
   silent.
3. **It must not feed itself.** A failed insert logs, and that log would be captured, and its
   failure would log... The writer thread marks itself, and records from this module are always
   ignored.

A dedicated psycopg2 connection (not the async engine) is used deliberately: `logging` is
called from sync code, background threads, and exception handlers where no event loop is
available or safe to touch.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
import traceback
import uuid

from app.core.config import settings

_MODULE_LOGGER = __name__
_QUEUE_MAX = 2000
_BATCH = 50
_FLUSH_SECONDS = 2.0

# `clog()` writes `call.<phase> linkedid=<id> channel=<id> k=v`; pulling the linkedid out lets
# /errors answer "what went wrong on this call" without full-text searching.
_LINKEDID_RE = re.compile(r"\blinkedid=([^\s]+)")

_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
_writer_local = threading.local()
_dropped = 0
_installed = False


class PostgresLogHandler(logging.Handler):
    """Enqueue serious records for the writer thread. Does no I/O itself."""

    def __init__(self, service: str, level: int) -> None:
        super().__init__(level=level)
        self.service = service

    def emit(self, record: logging.LogRecord) -> None:
        global _dropped
        try:
            # Never capture our own output, and never capture anything emitted from inside the
            # writer thread — either would be a feedback loop.
            if record.name.startswith(_MODULE_LOGGER) or getattr(_writer_local, "writing", False):
                return
            message = record.getMessage()
            tb = None
            if record.exc_info:
                tb = "".join(traceback.format_exception(*record.exc_info))[:20000]
            match = _LINKEDID_RE.search(message)
            _queue.put_nowait({
                "id": str(uuid.uuid4()),
                "service": self.service,
                "level": record.levelname,
                "logger": record.name[:255],
                "message": message[:10000],
                "linkedid": match.group(1)[:255] if match else None,
                "traceback": tb,
            })
        except queue.Full:
            # Losing log mirroring is always better than slowing down a call.
            _dropped += 1
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


def _dsn() -> str:
    """libpq DSN for psycopg2 — the async URL with the SQLAlchemy driver marker removed."""
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://")


def _writer_loop() -> None:
    import psycopg2  # imported lazily so importing this module never requires the driver

    _writer_local.writing = True
    conn = None
    pending: list[dict] = []
    while True:
        try:
            deadline = time.monotonic() + _FLUSH_SECONDS
            while len(pending) < _BATCH:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    break
                try:
                    pending.append(_queue.get(timeout=timeout))
                except queue.Empty:
                    break
            if not pending:
                continue

            if conn is None or conn.closed:
                conn = psycopg2.connect(_dsn())
                conn.autocommit = True
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO app_logs (id, at, service, level, logger, message, linkedid, "
                    "traceback) VALUES (%(id)s, now(), %(service)s, %(level)s, %(logger)s, "
                    "%(message)s, %(linkedid)s, %(traceback)s)",
                    pending,
                )
            pending.clear()
        except Exception:  # noqa: BLE001 - the mirror is best-effort, always
            # Drop this batch and back off. Retrying forever on a batch that Postgres refuses
            # (e.g. before the migration has run) would wedge the thread permanently.
            pending.clear()
            try:
                if conn is not None and not conn.closed:
                    conn.close()
            except Exception:  # noqa: BLE001
                pass
            conn = None
            time.sleep(5.0)


def install(service: str) -> None:
    """Attach the handler to the root logger and start the writer thread. Idempotent.

    `service` is 'app' or 'worker' — both containers run the same image, and knowing which one
    produced a record is most of the value when reading /api/ai/errors.
    """
    global _installed
    if _installed:
        return
    level_name = str(settings.APP_LOG_CAPTURE_LEVEL or "WARNING").strip().upper()
    resolved = logging.getLevelName(level_name)
    level = resolved if isinstance(resolved, int) else logging.WARNING

    logging.getLogger().addHandler(PostgresLogHandler(service, level))
    threading.Thread(target=_writer_loop, name="app-log-capture", daemon=True).start()
    _installed = True
    logging.getLogger(_MODULE_LOGGER).info(
        "app_logs capture installed for service=%s at level=%s", service, level_name
    )


def dropped_count() -> int:
    """How many records the queue refused. Surfaced by /api/ai/errors so a gap is visible."""
    return _dropped
