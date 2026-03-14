"""
db/zones.py — Работа с таблицей zones (зоны влияния Orchestrator).

Экспортирует:
    get_zone(name)          → dict с текущим состоянием зоны
    get_all_zones()         → dict {zone_name: {...}}
    update_zone_score(...)  → изменить confidence_score и статус
    apply_zone_decay()      → пассивное снижение score для неактивных зон
    is_zone_active(name)    → bool — можно ли применять изменения в этой зоне
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

import config
from db.connection import get_db

logger = logging.getLogger(__name__)

ZONE_NAMES = ("scheduling", "visual", "prelend", "code")

# Дата последнего применения деградации — не более одного раза в сутки
_last_decay_date: Optional[str] = None


def get_zone(zone_name: str) -> Optional[Dict]:
    """Возвращает состояние зоны или None если зона не найдена."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM zones WHERE zone_name = ?", (zone_name,)
        ).fetchone()
    return dict(row) if row else None


def get_all_zones() -> Dict[str, Dict]:
    """Возвращает все зоны как {zone_name: {...}}."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM zones ORDER BY zone_name").fetchall()
    return {row["zone_name"]: dict(row) for row in rows}


def update_zone_score(
    zone_name: str,
    delta: int,
    reason: str = "",
    mark_applied: bool = False,
) -> Dict:
    """
    Изменяет confidence_score на delta (может быть отрицательным).
    Если score выходит за пределы 0-100 — обрезается.
    Автоматически включает/выключает зону по порогам из config:
        score >= ZONE_ACTIVATE_THRESHOLD   → enabled = True
        score <  ZONE_DEACTIVATE_THRESHOLD → enabled = False
    mark_applied=True → обновляет last_applied_at (для сброса счётчика деградации).
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT confidence_score, enabled FROM zones WHERE zone_name = ?",
            (zone_name,)
        ).fetchone()
        if row is None:
            logger.warning("[Zones] Неизвестная зона: %s", zone_name)
            return {}

        old_score = row["confidence_score"]
        new_score = max(0, min(100, old_score + delta))

        # Логика hysteresis: для включения нужен высокий порог,
        # для выключения — низкий. Это предотвращает мигание зон.
        old_enabled = bool(row["enabled"])
        if not old_enabled and new_score >= config.ZONE_ACTIVATE_THRESHOLD:
            new_enabled = True
            logger.info("[Zones] %s ВКЛЮЧЕНА (score %d→%d)", zone_name, old_score, new_score)
        elif old_enabled and new_score < config.ZONE_DEACTIVATE_THRESHOLD:
            new_enabled = False
            logger.warning("[Zones] %s ВЫКЛЮЧЕНА (score %d→%d) — %s",
                           zone_name, old_score, new_score, reason)
        else:
            new_enabled = old_enabled

        now_iso = datetime.now().isoformat(timespec="seconds")
        update_fields = {
            "confidence_score": new_score,
            "enabled":          int(new_enabled),
            "last_changed_at":  now_iso,
        }
        if mark_applied:
            update_fields["last_applied_at"] = now_iso

        if mark_applied:
            conn.execute("""
                UPDATE zones
                SET confidence_score = :confidence_score,
                    enabled          = :enabled,
                    last_changed_at  = :last_changed_at,
                    last_applied_at  = :last_applied_at
                WHERE zone_name = :zone_name
            """, {**update_fields, "zone_name": zone_name})
        else:
            conn.execute("""
                UPDATE zones
                SET confidence_score = :confidence_score,
                    enabled          = :enabled,
                    last_changed_at  = :last_changed_at
                WHERE zone_name = :zone_name
            """, {**update_fields, "zone_name": zone_name})

    logger.debug("[Zones] %s: score %d→%d (%+d) %s",
                 zone_name, old_score, new_score, delta, reason)
    return get_zone(zone_name)


def set_zone_enabled(zone_name: str, enabled: bool, reason: str = "") -> None:
    """
    Принудительно включает или выключает зону (команда оператора).
    Не меняет confidence_score — только enabled флаг.
    """
    with get_db() as conn:
        conn.execute(
            "UPDATE zones SET enabled = ?, last_changed_at = ?, notes = ? WHERE zone_name = ?",
            (int(enabled), datetime.now().isoformat(timespec="seconds"), reason, zone_name)
        )
    action = "ВКЛЮЧЕНА" if enabled else "ВЫКЛЮЧЕНА"
    logger.info("[Zones] %s %s вручную: %s", zone_name, action, reason)


def apply_zone_decay() -> None:
    """
    Пассивная деградация confidence_score для зон, которые давно не применялись.

    Логика:
        Если last_applied_at IS NULL или (now - last_applied_at) > ZONE_DECAY_DAYS дней
        → снижаем score на ZONE_DECAY_PER_DAY за каждый день сверх порога.

    Вызывается в начале каждого цикла Orchestrator, но выполняется не чаще одного раза в сутки.
    """
    global _last_decay_date
    now      = datetime.now()
    today    = now.date().isoformat()
    if _last_decay_date == today:
        return
    _last_decay_date = today

    all_zones = get_all_zones()

    for zone_name, zone in all_zones.items():
        last_applied = zone.get("last_applied_at")
        if not last_applied:
            # Зона никогда не применялась — деградация начинается с момента создания
            last_applied = zone.get("last_changed_at", now.isoformat())

        try:
            last_dt  = datetime.fromisoformat(last_applied)
            days_old = (now - last_dt).total_seconds() / 86400
        except Exception:
            continue

        if days_old > config.ZONE_DECAY_DAYS:
            decay_days  = int(days_old - config.ZONE_DECAY_DAYS)
            decay_total = decay_days * config.ZONE_DECAY_PER_DAY
            if decay_total > 0:
                update_zone_score(
                    zone_name,
                    delta=-decay_total,
                    reason=f"пассивная деградация: {decay_days} дней без применения",
                )


def is_zone_active(zone_name: str) -> bool:
    """
    Возвращает True если зона включена И не заморожена оператором.
    Это главная проверка перед применением любого изменения.
    """
    from db.commands import is_zone_frozen
    zone = get_zone(zone_name)
    if not zone:
        return False
    if is_zone_frozen(zone_name):
        return False
    return bool(zone["enabled"])
