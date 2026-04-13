"""
db/connection.py — Подключение к SQLite и инициализация схемы.

Использование:
    from db.connection import get_db
    with get_db() as conn:
        conn.execute("SELECT ...")
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import config

logger = logging.getLogger(__name__)

_SCHEMA_FILE = Path(__file__).parent / "schema.sql"


def _migrate_applied_changes_commit_hash(conn: sqlite3.Connection) -> None:
    """Добавляет колонку commit_hash в applied_changes для существующих БД."""
    rows = conn.execute("PRAGMA table_info(applied_changes)").fetchall()
    cols = {r[1] for r in rows}
    if "commit_hash" not in cols:
        conn.execute("ALTER TABLE applied_changes ADD COLUMN commit_hash TEXT")


def _migrate_plan_quality_llm_judge(conn: sqlite3.Connection) -> None:
    """Колонки LLM-as-judge для plan_quality_scores."""
    rows = conn.execute("PRAGMA table_info(plan_quality_scores)").fetchall()
    cols = {r[1] for r in rows}
    if "llm_judge_score" not in cols:
        conn.execute("ALTER TABLE plan_quality_scores ADD COLUMN llm_judge_score INTEGER")
    if "llm_judge_reasoning" not in cols:
        conn.execute("ALTER TABLE plan_quality_scores ADD COLUMN llm_judge_reasoning TEXT")


def _migrate_agent_events_registry(conn: sqlite3.Connection) -> None:
    """Гарантирует актуальную структуру agent_events в существующих БД."""
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

    rows = conn.execute("PRAGMA table_info(agent_events)").fetchall()
    cols = {r[1] for r in rows}
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


def init_db() -> None:
    """
    Создаёт БД и применяет schema.sql если таблицы ещё не существуют.
    Вызывается один раз при старте Orchestrator.
    """
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")

        schema_sql = _SCHEMA_FILE.read_text(encoding="utf-8")
        # Выполняем весь schema.sql (CREATE TABLE IF NOT EXISTS — идемпотентно)
        conn.executescript(schema_sql)
        _migrate_applied_changes_commit_hash(conn)
        _migrate_plan_quality_llm_judge(conn)
        _migrate_agent_events_registry(conn)
        conn.commit()
        logger.info("[DB] Инициализирована: %s", config.DB_PATH)
    finally:
        conn.close()


@contextmanager
def get_db():
    """
    Контекстный менеджер для работы с БД.
    Автоматически коммитит при выходе без исключений, откатывает при ошибке.

    Пример:
        with get_db() as conn:
            conn.execute("INSERT INTO notifications ...")
    """
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
