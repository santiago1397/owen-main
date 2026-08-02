"""Keep the `/api/ai/query` read-only Postgres role correctly granted.

The role itself must be created by hand, once:

    sudo -u postgres psql -c "CREATE ROLE owen_ro LOGIN PASSWORD '...' NOSUPERUSER \\
        NOCREATEDB NOCREATEROLE NOINHERIT;"

That step cannot be automated from the container — creating a role needs CREATEROLE, which the
application's database user deliberately does not have. Everything *after* it can be, and is:
the app user owns the tables, so it can grant SELECT on them.

Grants are re-applied at every startup rather than once in a migration, because `GRANT SELECT
ON ALL TABLES` only covers tables that exist at the moment it runs. Any future migration that
adds a table would silently leave it unreadable, and the failure would surface much later as a
confusing permission error inside someone's query. `ALTER DEFAULT PRIVILEGES` covers tables
created *by this role* from now on; re-running the blanket grant covers everything else.

The REVOKEs are the actual security boundary and run last, so they can never be undone by the
grant above them:

- `users`          — password hashes
- `api_keys`       — key hashes; readable hashes would let a leaked read turn into forged auth
- `api_key_usage`  — the audit trail of the very keys that run these queries

Everything here is best-effort. A deployment where the role was never created must start
normally with `/query` disabled, not fail to boot.
"""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, text

from app.core.config import settings

logger = logging.getLogger(__name__)

# Tables the read-only role must never see, whatever else it is granted.
SECRET_TABLES = ("users", "api_keys", "api_key_usage")


def sync_grants() -> bool:
    """Re-apply grants/revokes for POSTGRES_RO_USER. Returns True if they were applied."""
    role = (settings.POSTGRES_RO_USER or "").strip()
    if not role:
        return False
    # The role name is interpolated into DDL because Postgres does not accept a bind parameter
    # for an identifier. Restrict it to a safe identifier shape so a hostile .env value cannot
    # become SQL — this comes from our own config, but the cost of being strict is nil.
    if not role.replace("_", "").isalnum():
        logger.error("POSTGRES_RO_USER %r is not a plain identifier; refusing to run DDL", role)
        return False

    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
            ).first()
            if not exists:
                logger.warning(
                    "read-only role %r does not exist; /api/ai/query will stay disabled. "
                    "Create it once with: CREATE ROLE %s LOGIN PASSWORD '...' NOSUPERUSER "
                    "NOCREATEDB NOCREATEROLE NOINHERIT;", role, role,
                )
                return False

            conn.execute(text(f'GRANT CONNECT ON DATABASE "{settings.POSTGRES_DB}" TO "{role}"'))
            conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
            conn.execute(text(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{role}"'))
            conn.execute(text(f'GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO "{role}"'))
            # Covers tables created from here on by the app user (i.e. by future migrations).
            conn.execute(text(
                f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO "{role}"'
            ))
            # Last, and therefore final.
            for table in SECRET_TABLES:
                conn.execute(text(f'REVOKE ALL ON TABLE "{table}" FROM "{role}"'))
        logger.info("read-only role %r grants synced (secrets revoked: %s)",
                    role, ", ".join(SECRET_TABLES))
        return True
    except Exception:  # noqa: BLE001 - never prevent startup over an optional capability
        logger.exception("failed to sync grants for read-only role %r; /api/ai/query may fail", role)
        return False
    finally:
        engine.dispose()
