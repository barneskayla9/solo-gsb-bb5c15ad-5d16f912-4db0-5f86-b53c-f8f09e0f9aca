"""SQLite connection management and schema for the canary gate."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.getenv("CANARY_DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "canary.db"))

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS services (
    name            TEXT PRIMARY KEY,
    description     TEXT NOT NULL DEFAULT '',
    default_rollout TEXT NOT NULL DEFAULT '[5, 10, 25, 50, 75, 100]',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS releases (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    service_name             TEXT NOT NULL REFERENCES services(name) ON DELETE CASCADE,
    version                  TEXT NOT NULL,
    percentage               INTEGER NOT NULL CHECK (percentage BETWEEN 0 AND 100),
    stages                   TEXT NOT NULL DEFAULT '[100]',
    stage_index              INTEGER NOT NULL DEFAULT 0,
    error_threshold          REAL NOT NULL DEFAULT 0.1,
    window_seconds           INTEGER NOT NULL DEFAULT 300,
    min_requests             INTEGER NOT NULL DEFAULT 10,
    status                   TEXT NOT NULL DEFAULT 'progressing'
        CHECK (status IN ('progressing', 'paused', 'complete', 'rolled_back')),
    tenant_allowlist         TEXT NOT NULL DEFAULT '[]',
    tenant_denylist          TEXT NOT NULL DEFAULT '[]',
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    UNIQUE (service_name, version)
);
CREATE INDEX IF NOT EXISTS idx_releases_service_status ON releases(service_name, status);

CREATE TABLE IF NOT EXISTS telemetry (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    service_name  TEXT NOT NULL,
    release_id    INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
    user_id       TEXT,
    success       INTEGER NOT NULL CHECK (success IN (0, 1)),
    latency_ms    REAL,
    status_code   INTEGER,
    occurred_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_release_time ON telemetry(release_id, occurred_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    service_name TEXT NOT NULL,
    action       TEXT NOT NULL,
    version      TEXT,
    actor        TEXT NOT NULL DEFAULT 'api',
    detail       TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_service_time ON audit_log(service_name, created_at);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""

# --------------------------------------------------------------------------- #
# Migrations for databases created by earlier versions.
# Each migration is idempotent via _column_exists, so it is also safe to run
# against a freshly-created schema that already includes the columns.
# --------------------------------------------------------------------------- #
MIGRATIONS = [
    # version 1: tenant-targeted rollout lists on releases
    (
        1,
        [
            "ALTER TABLE releases ADD COLUMN tenant_allowlist TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE releases ADD COLUMN tenant_denylist TEXT NOT NULL DEFAULT '[]'",
        ],
    ),
]


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _run_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    for version, statements in MIGRATIONS:
        if version in applied:
            continue
        for stmt in statements:
            # ALTER TABLE ADD COLUMN has no IF NOT EXISTS in SQLite; guard manually.
            head, _, _ = stmt.partition(" ADD COLUMN ")
            table = head.split()[-1]
            column = stmt.split("ADD COLUMN", 1)[1].split()[0]
            if not _column_exists(conn, table, column):
                conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        _run_migrations(conn)
        conn.commit()
    finally:
        conn.close()


def get_db():
    """FastAPI yield-dependency: commit on success, roll back on error."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
