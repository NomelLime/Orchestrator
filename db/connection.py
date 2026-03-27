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
