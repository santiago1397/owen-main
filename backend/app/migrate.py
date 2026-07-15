"""Run Alembic migrations behind a Postgres advisory lock so that with the
app + worker containers both starting, only one migrates and the other waits
(ARCHITECTURE.md #15). Called at startup by both entrypoints.
"""

import logging

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.core.config import settings

logger = logging.getLogger("migrate")
_LOCK_KEY = 0x0CA11  # arbitrary constant shared by every process


def run_migrations() -> None:
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    # A short-lived sync engine only for the lock + alembic (alembic is sync).
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _LOCK_KEY})
        try:
            cfg = Config("alembic.ini")
            cfg.set_main_option("sqlalchemy.url", sync_url)
            command.upgrade(cfg, "head")
            logger.info("migrations applied to head")
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
    engine.dispose()
