"""Postgres connection pool + migration runner for the FastAPI portal.

`DATABASE_URL` is injected by ECS from Secrets Manager (the `url` JSON key on
the project's DB secret). When unset, the pool is disabled and the rest of
the portal still boots — useful for local dev where the team isn't running
Postgres. Health checks at `/api/db/health` surface whichever state we're in.

Runtime behaviour:
    * `init_pool()` is called once on FastAPI startup.
    * It opens with `open=False` so an unreachable DB doesn't crash boot;
      the first connection attempt happens lazily on the first request.
    * If `RUN_MIGRATIONS=1`, every `*.sql` file in the migrations dir is
      applied in lexicographic order. Already-applied scripts are recorded
      in a `_migrations` table so re-runs are no-ops.
    * Pool size is intentionally small. The portal has a handful of concurrent
      users at most, and each search query holds a connection only for the
      duration of one ANN scan.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
RUN_MIGRATIONS = os.getenv("RUN_MIGRATIONS", "0") == "1"
DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "8"))
DB_STATEMENT_TIMEOUT_MS = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "15000"))

_pool: Optional[ConnectionPool] = None


def init_pool() -> Optional[ConnectionPool]:
    """Open the connection pool if DATABASE_URL is set. Idempotent."""
    global _pool
    if _pool is not None:
        return _pool
    if not DATABASE_URL:
        logger.info("DATABASE_URL not set; Postgres pool disabled")
        return None

    _pool = ConnectionPool(
        DATABASE_URL,
        min_size=DB_POOL_MIN,
        max_size=DB_POOL_MAX,
        open=False,
        kwargs={
            "autocommit": True,
            "options": f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}",
        },
        name="eihp-portal",
    )
    try:
        _pool.open(wait=True, timeout=10.0)
        logger.info("Postgres pool opened")
    except Exception as exc:  # noqa: BLE001
        # Don't crash boot on transient unreachability; the health check will
        # surface it and ECS rollouts can complete even if the DB is briefly
        # offline (e.g. during a parameter-group reboot).
        logger.warning("Postgres pool open failed (%s); will retry on demand", exc)
    return _pool


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("Postgres pool is not initialized; DATABASE_URL not set")
    return _pool


def is_enabled() -> bool:
    return _pool is not None


def run_migrations(migrations_dir: Path) -> list[str]:
    """Apply *.sql files in name order, idempotently.

    Each file is wrapped in a transaction. The applied set is tracked in
    `_migrations(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ)`; we don't
    re-run anything that's already there. SQL files themselves should still
    be written defensively (CREATE … IF NOT EXISTS) so manual fixups are easy.
    """
    if _pool is None:
        raise RuntimeError("DATABASE_URL not set; cannot run migrations")

    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        logger.info("No migration files found in %s", migrations_dir)
        return []

    applied: list[str] = []
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS _migrations (
                    name        TEXT PRIMARY KEY,
                    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute("SELECT name FROM _migrations")
            already = {row[0] for row in cur.fetchall()}

        for path in files:
            if path.name in already:
                continue
            sql = path.read_text()
            logger.info("Applying migration %s (%d bytes)", path.name, len(sql))
            # Use a fresh connection per migration so a failure cleanly rolls
            # back without affecting subsequent ones.
            with _pool.connection() as mig_conn:
                # autocommit=True at the pool level; switch to transactional
                # mode for the migration body itself.
                mig_conn.autocommit = False
                try:
                    with mig_conn.cursor() as cur:
                        cur.execute(sql)
                        cur.execute(
                            "INSERT INTO _migrations (name) VALUES (%s)",
                            (path.name,),
                        )
                    mig_conn.commit()
                except Exception:
                    mig_conn.rollback()
                    raise
                finally:
                    mig_conn.autocommit = True
            applied.append(path.name)
    return applied


def health() -> dict:
    """Cheap probe used by /api/db/health. Never raises."""
    if _pool is None:
        return {"status": "disabled", "detail": "DATABASE_URL not set"}
    try:
        with _pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                version = cur.fetchone()[0]
                cur.execute(
                    "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
                )
                row = cur.fetchone()
                pgvector_version = row[0] if row else None
                cur.execute("SELECT count(*) FROM videos")
                n_videos = cur.fetchone()[0]
                cur.execute(
                    "SELECT count(*) FILTER (WHERE kind = 'clip'), "
                    "       count(*) FILTER (WHERE kind = 'frame') "
                    "FROM embeddings"
                )
                n_clips, n_frames = cur.fetchone()
        # Trim the verbose Postgres banner to the first comma so the JSON
        # stays readable; the full version is in the logs anyway.
        return {
            "status": "ok",
            "postgres": version.split(",", 1)[0],
            "pgvector": pgvector_version,
            "videos": int(n_videos),
            "clips": int(n_clips),
            "frames": int(n_frames),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "detail": f"{exc.__class__.__name__}: {exc}",
        }


def close_pool() -> None:
    global _pool
    if _pool is None:
        return
    try:
        _pool.close()
    except Exception:  # noqa: BLE001
        logger.exception("error closing pool")
    _pool = None
