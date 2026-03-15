"""
modules/agent_healer.py — Self-healing: откат конфига агента при краш-лупе.

Алгоритм:
  1. `snapshot_config(agent_name, config_file, plan_id)` — вызывается из
     config_enforcer.py ПЕРЕД применением изменения → сохраняет снимок в БД
  2. `check_and_heal(agent_name, crash_window_minutes)` — вызывается
     в main_orchestrator.py на каждом цикле:
     - Ищет crash-события агента в agent_memory.json
     - Если ≥2 крашей за crash_window_minutes И есть изменение конфига за этот период
       → восстанавливает предыдущий снапшот
     - Логирует в notifications
  3. `get_snapshots(agent_name, limit)` — для ContentHub: история снапшотов
  4. `restore_snapshot(snapshot_id)` — ручной откат из ContentHub UI

Не затрагивает git-историю кода — только конфиг-файлы.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import config
from db.connection import get_db

logger = logging.getLogger(__name__)

# Минимальный интервал между откатами одного агента (чтобы не зациклиться)
_MIN_HEAL_INTERVAL_MIN = 30


# ── Публичный API ──────────────────────────────────────────────────────────

def snapshot_config(
    agent_name: str,
    config_file: str,
    plan_id: Optional[int] = None,
) -> int:
    """
    Сохраняет снапшот конфига перед изменением.
    Вызывать до атомарной записи.

    Returns:
        snapshot_id (int) или 0 при ошибке
    """
    try:
        path = Path(config_file)
        if not path.exists():
            logger.debug("[AgentHealer] Файл не найден для снапшота: %s", config_file)
            return 0

        content = path.read_text(encoding="utf-8")

        with get_db() as conn:
            cur = conn.execute(
                """
                INSERT INTO agent_config_snapshots
                    (agent_name, config_file, config_json, applied_plan_id)
                VALUES (?, ?, ?, ?)
                """,
                (agent_name, str(path.resolve()), content, plan_id),
            )
            snap_id = cur.lastrowid

        logger.debug(
            "[AgentHealer] Снапшот #%d сохранён: %s (%s)",
            snap_id, agent_name, path.name,
        )
        return snap_id

    except Exception as exc:
        logger.warning("[AgentHealer] Ошибка создания снапшота: %s", exc)
        return 0


def check_and_heal(
    agent_name: str,
    crash_window_minutes: int = 30,
    min_crashes: int = 2,
) -> bool:
    """
    Проверяет наличие краш-лупа для агента и откатывает конфиг.

    Returns:
        True если был выполнен откат, False иначе.
    """
    # Читаем agent_memory.json SP для статусов агентов
    crashes = _count_recent_crashes(agent_name, crash_window_minutes)
    if crashes < min_crashes:
        return False

    logger.warning(
        "[AgentHealer] Обнаружен краш-луп агента %s: %d крашей за %d мин",
        agent_name, crashes, crash_window_minutes,
    )

    # Ищем последнее изменение конфига за период краша
    since = (datetime.utcnow() - timedelta(minutes=crash_window_minutes)).isoformat()
    snap = _find_snapshot_before(agent_name, since)

    if snap is None:
        logger.info("[AgentHealer] Нет снапшота конфига для %s — откат невозможен", agent_name)
        return False

    # Проверяем минимальный интервал между откатами
    if _recently_healed(agent_name):
        logger.info("[AgentHealer] Откат %s уже был недавно — пропускаем", agent_name)
        return False

    ok = _restore(snap)
    if ok:
        _notify_heal(agent_name, snap, crashes)
    return ok


def restore_snapshot(snapshot_id: int) -> bool:
    """
    Ручной откат к конкретному снапшоту (из ContentHub UI).
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM agent_config_snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()

    if not row:
        logger.warning("[AgentHealer] Снапшот #%d не найден", snapshot_id)
        return False

    ok = _restore(dict(row))
    if ok:
        logger.info("[AgentHealer] Ручной откат к снапшоту #%d", snapshot_id)
    return ok


def get_snapshots(agent_name: Optional[str] = None, limit: int = 50) -> List[Dict]:
    """Возвращает историю снапшотов для ContentHub."""
    with get_db() as conn:
        if agent_name:
            rows = conn.execute(
                """
                SELECT id, created_at, agent_name, config_file, applied_plan_id
                FROM agent_config_snapshots
                WHERE agent_name = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (agent_name, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, created_at, agent_name, config_file, applied_plan_id
                FROM agent_config_snapshots
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


# ── Внутренние функции ────────────────────────────────────────────────────


def _count_recent_crashes(agent_name: str, window_minutes: int) -> int:
    """
    Считает количество ERROR-событий агента в agent_memory.json за последние N минут.
    """
    # Проверяем SP agent_memory
    crash_count = 0
    for mem_path in [config.SP_AGENT_MEMORY, config.PL_AGENT_MEMORY, config.ORC_AGENT_MEMORY]:
        path = Path(mem_path)
        if not path.exists():
            continue
        try:
            data   = json.loads(path.read_text(encoding="utf-8"))
            events = data.get("events", [])
            since  = (datetime.utcnow() - timedelta(minutes=window_minutes)).isoformat()
            for ev in events:
                if (
                    ev.get("agent") == agent_name
                    and ev.get("type") in ("error", "crash", "exception")
                    and ev.get("ts", "") >= since
                ):
                    crash_count += 1
        except Exception:
            pass

    return crash_count


def _find_snapshot_before(agent_name: str, since_iso: str) -> Optional[Dict]:
    """
    Ищет предпоследний снапшот конфига агента (до периода краша).
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM agent_config_snapshots
            WHERE agent_name = ?
            ORDER BY created_at DESC
            LIMIT 5
            """,
            (agent_name,),
        ).fetchall()

    if not rows:
        return None

    # Ищем снапшот сделанный ДО периода краша (самый свежий до since)
    for row in rows:
        if row["created_at"] < since_iso:
            return dict(row)

    # Если все снапшоты во время краша — берём самый старый из имеющихся
    if rows:
        return dict(rows[-1])

    return None


def _restore(snap: Dict) -> bool:
    """Восстанавливает конфиг-файл из снапшота."""
    config_file = snap.get("config_file", "")
    config_json = snap.get("config_json", "")

    if not config_file or not config_json:
        return False

    path = Path(config_file)
    try:
        # Атомарная запись (паттерн из config_enforcer.py)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            os.write(fd, config_json.encode("utf-8"))
            os.close(fd)
            os.replace(tmp, str(path))
        except Exception:
            try:
                os.close(fd)
                os.unlink(tmp)
            except OSError:
                pass
            raise

        logger.info(
            "[AgentHealer] Восстановлен конфиг %s (снапшот #%d от %s)",
            path.name, snap.get("id"), snap.get("created_at"),
        )
        return True

    except Exception as exc:
        logger.error("[AgentHealer] Ошибка восстановления конфига: %s", exc)
        return False


def _recently_healed(agent_name: str) -> bool:
    """Проверяет был ли откат за последние _MIN_HEAL_INTERVAL_MIN минут."""
    since = (datetime.utcnow() - timedelta(minutes=_MIN_HEAL_INTERVAL_MIN)).isoformat()
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM notifications
            WHERE category = 'heal'
              AND message LIKE ?
              AND created_at >= ?
            LIMIT 1
            """,
            (f"%{agent_name}%", since),
        ).fetchone()
    return row is not None


def _notify_heal(agent_name: str, snap: Dict, crashes: int) -> None:
    """Записывает уведомление о восстановлении в БД."""
    msg = (
        f"🔧 [Self-healing] Откат конфига {agent_name}: "
        f"снапшот #{snap.get('id')} от {snap.get('created_at')[:16]} "
        f"(причина: {crashes} крашей)"
    )
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO notifications (level, category, message) VALUES ('warning', 'heal', ?)",
                (msg,),
            )
    except Exception as exc:
        logger.warning("[AgentHealer] Ошибка записи уведомления: %s", exc)
    logger.warning(msg)
