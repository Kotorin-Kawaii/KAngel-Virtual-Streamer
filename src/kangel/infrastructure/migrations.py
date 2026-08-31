"""Small, explicit SQLite migrations used by the single-process server.

The project intentionally does not depend on Alembic.  P24 nevertheless needs a
real migration contract because the production database predates its retry
metadata.  Each migration is idempotent and records its own marker in
``schema_migrations``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
import sqlite3


P24_RELIABILITY_MIGRATION = "stream_episodic_memory_reliability_v1"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def upgrade_stream_memory_reliability_v1(conn: sqlite3.Connection) -> None:
    """Upgrade old P24 tables without rewriting existing business data."""

    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    if conn.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_id = ?",
        (P24_RELIABILITY_MIGRATION,),
    ).fetchone():
        return

    # Candidate lifecycle metadata.  ``pending`` remains a valid state only
    # when a task can claim it; terminal decisions are explicit ``discarded``.
    _add_column(conn, "stream_memory_candidates", "claim_token", "TEXT")
    _add_column(conn, "stream_memory_candidates", "resolved_at", "TEXT")
    _add_column(conn, "stream_memory_candidates", "resolution_code", "TEXT")
    _add_column(conn, "stream_memory_candidates", "last_error_code", "TEXT")

    # Task execution and batch/retry metadata.  Defaults make this compatible
    # with the real pre-reliability production schema.
    _add_column(conn, "stream_memory_tasks", "next_attempt_at", "TEXT")
    _add_column(conn, "stream_memory_tasks", "claim_token", "TEXT")
    _add_column(
        conn, "stream_memory_tasks", "current_batch_candidate_ids_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _add_column(
        conn, "stream_memory_tasks", "reflection_fragments_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _add_column(conn, "stream_memory_tasks", "last_error_detail", "TEXT")
    _add_column(
        conn, "stream_memory_tasks", "last_error_retryable",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _add_column(
        conn, "stream_memory_tasks", "recovery_count",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _add_column(
        conn, "stream_memory_tasks", "batch_size",
        "INTEGER NOT NULL DEFAULT 8",
    )
    _add_column(
        conn, "stream_memory_tasks", "batch_index",
        "INTEGER NOT NULL DEFAULT 0",
    )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_tasks_retry "
        "ON stream_memory_tasks(status, next_attempt_at, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_candidates_claim "
        "ON stream_memory_candidates(status, claim_token, occurred_at)"
    )

    # The old implementation called exhausted transient failures ``failed``.
    # They are not evidence of a permanent semantic failure, so make them
    # recoverable and immediately eligible for one bounded replay.
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE stream_memory_tasks SET status='failed_retryable', "
        "completed_at=NULL, next_attempt_at=COALESCE(next_attempt_at, ?), "
        "last_error_retryable=1 WHERE status='failed'",
        (now,),
    )
    conn.execute(
        "INSERT INTO schema_migrations(migration_id) VALUES (?)",
        (P24_RELIABILITY_MIGRATION,),
    )


def downgrade_stream_memory_reliability_v1(conn: sqlite3.Connection) -> None:
    """Rebuild only P24 tables to their pre-v1 shape.

    This is intended for validation/rollback, not normal runtime use.  Core
    candidate/task data is preserved; reliability-only metadata is discarded.
    """

    marker = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_id = ?",
        (P24_RELIABILITY_MIGRATION,),
    ).fetchone()
    if not marker:
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("ALTER TABLE stream_memory_candidates RENAME TO _p24_candidates_v1")
    conn.execute("ALTER TABLE stream_memory_tasks RENAME TO _p24_tasks_v1")
    conn.execute("""
        CREATE TABLE stream_memory_candidates (
            candidate_id TEXT PRIMARY KEY,
            stream_session_id TEXT NOT NULL,
            scope TEXT NOT NULL,
            identity_type TEXT NOT NULL,
            account_id TEXT,
            event_type TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            topic TEXT NOT NULL DEFAULT '',
            salience REAL NOT NULL DEFAULT 0.0,
            valence REAL NOT NULL DEFAULT 0.0,
            appraisal_json TEXT NOT NULL DEFAULT '{}',
            occurred_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            UNIQUE(source_type, source_id)
        )
    """)
    conn.execute("""
        CREATE TABLE stream_memory_tasks (
            stream_session_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            lease_expires_at TEXT,
            candidate_ids_json TEXT NOT NULL,
            reflection_json TEXT,
            last_error_code TEXT,
            source_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
    """)
    candidate_columns = (
        "candidate_id, stream_session_id, scope, identity_type, account_id, "
        "event_type, source_type, source_id, topic, salience, valence, "
        "appraisal_json, occurred_at, status, created_at"
    )
    conn.execute(
        f"INSERT INTO stream_memory_candidates ({candidate_columns}) "
        f"SELECT {candidate_columns} FROM _p24_candidates_v1"
    )
    task_columns = (
        "stream_session_id, status, attempts, lease_expires_at, candidate_ids_json, "
        "reflection_json, last_error_code, source_version, created_at, updated_at, completed_at"
    )
    conn.execute(
        f"INSERT INTO stream_memory_tasks ({task_columns}) "
        f"SELECT {task_columns} FROM _p24_tasks_v1"
    )
    conn.execute("DROP TABLE _p24_candidates_v1")
    conn.execute("DROP TABLE _p24_tasks_v1")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_candidates_session "
        "ON stream_memory_candidates(stream_session_id, status, salience DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_tasks_status "
        "ON stream_memory_tasks(status, created_at)"
    )
    conn.execute(
        "DELETE FROM schema_migrations WHERE migration_id = ?",
        (P24_RELIABILITY_MIGRATION,),
    )
    conn.execute("PRAGMA foreign_keys = ON")
