#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return bool(row)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(r[1]) for r in rows}


def migrate(db_path: Path) -> None:
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                source_project  TEXT NOT NULL,
                agent_name      TEXT NOT NULL,
                event_type      TEXT NOT NULL,
                severity        TEXT NOT NULL DEFAULT 'info',
                creative_id     TEXT,
                hook_type       TEXT,
                experiment_id   TEXT,
                agent_run_id    TEXT,
                payload_json    TEXT NOT NULL DEFAULT '{}'
            )
            """
        )

        if _table_exists(conn, "agent_events"):
            cols = _table_columns(conn, "agent_events")
            if "source_project" not in cols:
                conn.execute("ALTER TABLE agent_events ADD COLUMN source_project TEXT")
            if "agent_name" not in cols:
                conn.execute("ALTER TABLE agent_events ADD COLUMN agent_name TEXT")
            if "event_type" not in cols:
                conn.execute("ALTER TABLE agent_events ADD COLUMN event_type TEXT")
            if "severity" not in cols:
                conn.execute("ALTER TABLE agent_events ADD COLUMN severity TEXT DEFAULT 'info'")
            if "creative_id" not in cols:
                conn.execute("ALTER TABLE agent_events ADD COLUMN creative_id TEXT")
            if "hook_type" not in cols:
                conn.execute("ALTER TABLE agent_events ADD COLUMN hook_type TEXT")
            if "experiment_id" not in cols:
                conn.execute("ALTER TABLE agent_events ADD COLUMN experiment_id TEXT")
            if "agent_run_id" not in cols:
                conn.execute("ALTER TABLE agent_events ADD COLUMN agent_run_id TEXT")
            if "payload_json" not in cols:
                conn.execute("ALTER TABLE agent_events ADD COLUMN payload_json TEXT DEFAULT '{}'")
            if "created_at" not in cols:
                conn.execute("ALTER TABLE agent_events ADD COLUMN created_at TEXT DEFAULT (datetime('now'))")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_events_created ON agent_events(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_events_creative ON agent_events(creative_id, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_events_experiment ON agent_events(experiment_id, created_at DESC)")

        conn.commit()
        print(f"[ok] migration applied: {db_path}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Idempotent migration for orchestrator.db")
    parser.add_argument("--db", required=True, help="Path to orchestrator.db")
    args = parser.parse_args()
    migrate(Path(args.db))


if __name__ == "__main__":
    main()
