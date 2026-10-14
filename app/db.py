"""Postgres storage.

Schema changes live in `migrations/NNN_name.sql` and are applied in order at startup.
Never edit an applied migration; add a new file instead.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_LOCK_ID = 7_242_001  # arbitrary; serializes migrations across app replicas

pool = ConnectionPool(
    settings.database_url,
    min_size=1,
    max_size=settings.db_pool_size,
    kwargs={"row_factory": dict_row},
    open=False,
)


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    """Pooled connection; commits on success, rolls back on error."""
    with pool.connection() as conn:
        yield conn


def run_migrations() -> None:
    with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_ID,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.stem in applied:
                    continue
                log.info("Applying migration %s", path.name)
                with conn.transaction():
                    conn.execute(path.read_text())
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_ID,))


def init_db() -> None:
    run_migrations()
    pool.open(wait=True, timeout=30)


def close_db() -> None:
    pool.close()
