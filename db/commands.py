"""
db/commands.py — Хранение и применение команд оператора.

Экспортирует:
    save_command(...)           → id команды
    get_pending_commands()      → список необработанных команд
    mark_command_applied(id)
    set_policy(key, value, ...)
    get_policy(key)             → значение политики или None
    get_all_policies()          → dict {key: value}
    is_zone_frozen(zone_name)   → bool
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db.connection import get_db

logger = logging.getLogger(__name__)


def _is_expired(expires_at_str: str) -> bool:
    """Проверяет истечение политики. Безопасно для naive и aware datetime."""
    try:
        dt = datetime.fromisoformat(expires_at_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < datetime.now(timezone.utc)
    except Exception:
        return False


def save_command(
    raw_text: str,
    parsed_json: Optional[Dict] = None,
    command_type: Optional[str] = None,
) -> int:
    """Сохраняет команду оператора. Возвращает id."""
    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO operator_commands (raw_text, parsed_json, command_type)
            VALUES (?, ?, ?)
        """, (
            raw_text,
            json.dumps(parsed_json, ensure_ascii=False) if parsed_json else None,
            command_type,
        ))
        return cursor.lastrowid


def get_pending_commands() -> List[Dict]:
    """Возвращает необработанные команды оператора."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM operator_commands WHERE status = 'pending' ORDER BY received_at"
        ).fetchall()
    return [dict(row) for row in rows]


def mark_command_applied(command_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE operator_commands SET status = 'applied', applied_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), command_id)
        )


def mark_command_rejected(command_id: int, reason: str = "") -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE operator_commands SET status = 'rejected', notes = ? WHERE id = ?",
            (reason, command_id)
        )


def set_policy(
    key: str,
    value: Any,
    command_id: Optional[int] = None,
    expires_at: Optional[str] = None,
    description: str = "",
) -> None:
    """
    Устанавливает или обновляет политику оператора.
    Политики применяются при каждом цикле Orchestrator.

    Примеры ключей:
        'freeze_zone_visual'    → True/False
        'focus_geo'             → 'BR'
        'mode'                  → 'safe' | 'aggressive'
        'pause_evolution'       → True
    """
    with get_db() as conn:
        conn.execute("""
            INSERT INTO operator_policies (key, value_json, set_by_command, expires_at, description)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json      = excluded.value_json,
                set_at          = datetime('now'),
                set_by_command  = excluded.set_by_command,
                expires_at      = excluded.expires_at,
                description     = excluded.description
        """, (
            key,
            json.dumps(value, ensure_ascii=False),
            command_id,
            expires_at,
            description,
        ))
    logger.info("[Commands] Политика установлена: %s = %s", key, value)


def get_policy(key: str) -> Optional[Any]:
    """Возвращает значение политики или None если не установлена / истекла."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT value_json, expires_at FROM operator_policies
               WHERE key = ?""",
            (key,)
        ).fetchone()

    if row is None:
        return None

    # Проверяем срок действия
    if row["expires_at"]:
        if _is_expired(row["expires_at"]):
            return None

    try:
        return json.loads(row["value_json"])
    except Exception:
        return row["value_json"]


def get_all_policies() -> Dict[str, Any]:
    """Возвращает все активные (не истекшие) политики."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT key, value_json, expires_at FROM operator_policies"
        ).fetchall()

    policies = {}
    for row in rows:
        if row["expires_at"] and _is_expired(row["expires_at"]):
            continue
        try:
            policies[row["key"]] = json.loads(row["value_json"])
        except Exception:
            policies[row["key"]] = row["value_json"]
    return policies


def is_zone_frozen(zone_name: str) -> bool:
    """Проверяет, заморозил ли оператор данную зону."""
    return bool(get_policy(f"freeze_zone_{zone_name}"))


def cleanup_expired_policies() -> int:
    """Удаляет истекшие политики из БД. Возвращает количество удалённых."""
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM operator_policies WHERE expires_at IS NOT NULL AND expires_at < ?",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),)
        )
        deleted = cursor.rowcount
    if deleted:
        logger.info("[Commands] Удалено истекших политик: %d", deleted)
    return deleted
