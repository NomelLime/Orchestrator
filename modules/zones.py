"""
modules/zones.py — Логика управления зонами доверия.

Этот модуль — «контролёр допуска»: перед любым применением изменений
evolution.py и config_enforcer.py спрашивают здесь, можно ли это делать.

Экспортирует:
    can_apply(zone_name)        → bool — можно ли применять изменения в зоне
    record_success(zone_name)   → повысить score за успешное применение
    record_failure(zone_name, reason) → понизить score за откат
    process_zone_commands()     → применить команды оператора к зонам
    get_zones_summary()         → строка для Telegram-сводки
"""

from __future__ import annotations

import logging
from typing import Dict

import config
from db.zones    import (
    get_all_zones, is_zone_active, update_zone_score, apply_zone_decay,
    set_zone_enabled, ZONE_NAMES,
)
from db.commands import get_pending_commands, mark_command_applied, is_zone_frozen, set_policy

logger = logging.getLogger(__name__)


def can_apply(zone_name: str) -> bool:
    """
    Главная проверка перед применением любого изменения.
    Зона должна быть:
        1. enabled=True (не выключена и не ниже DEACTIVATE_THRESHOLD)
        2. Не заморожена оператором через Telegram
        3. DRY_RUN=False

    Специальное правило для Zone 4 (code):
        Дополнительно проверяем что Zone 2 (visual) тоже активна и устойчива.
        Если visual нестабильна — code не трогаем.
    """
    if config.DRY_RUN:
        logger.info("[Zones] DRY_RUN: изменения в '%s' не применяются", zone_name)
        return False

    if is_zone_frozen(zone_name):
        logger.info("[Zones] Зона '%s' заморожена оператором", zone_name)
        return False

    if not is_zone_active(zone_name):
        logger.info("[Zones] Зона '%s' неактивна (score низкий или выключена)", zone_name)
        return False

    # Zone 4 (code) требует что Zone 2 (visual) тоже активна и стабильна
    if zone_name == "code":
        if not is_zone_active("visual"):
            logger.warning("[Zones] Zone 'code' заблокирована: зона 'visual' неактивна")
            return False

    return True


def record_success(zone_name: str, description: str = "") -> None:
    """Вызывается после успешного применения изменения в зоне."""
    update_zone_score(
        zone_name,
        delta      = config.ZONE_SCORE_SUCCESS_DELTA,
        reason     = f"успешное применение: {description}"[:80],
        mark_applied = True,
    )


def record_failure(zone_name: str, reason: str = "") -> None:
    """Вызывается при откате или провале тестов в зоне."""
    update_zone_score(
        zone_name,
        delta  = -config.ZONE_SCORE_FAILURE_DELTA,
        reason = f"откат: {reason}"[:80],
    )


def run_decay() -> None:
    """Запускает пассивную деградацию. Вызывается в начале каждого цикла."""
    apply_zone_decay()


def process_zone_commands() -> int:
    """
    Применяет pending-команды оператора, которые касаются зон.
    Возвращает количество обработанных команд.

    Команды типа:
        policy_update: freeze_zone_visual, enable_zone_prelend, etc.
        → записываются в operator_policies и применяются к zones.

    TODO: добавить полный парсинг parsed_json от LLM когда будет готов telegram_bot.py
    """
    import json
    commands = get_pending_commands()
    applied = 0

    for cmd in commands:
        parsed_str = cmd.get("parsed_json")
        if not parsed_str:
            continue

        try:
            parsed = json.loads(parsed_str) if isinstance(parsed_str, str) else parsed_str
        except Exception:
            continue

        cmd_type = parsed.get("type") or cmd.get("command_type")
        if cmd_type != "policy_update":
            continue

        action    = parsed.get("action", "")
        zone_name = parsed.get("zone")

        if not zone_name or zone_name not in ZONE_NAMES:
            continue

        if action in ("freeze_zone", "disable_zone"):
            set_policy(f"freeze_zone_{zone_name}", True,
                       command_id=cmd["id"],
                       description=f"заморожена из Telegram: {cmd['raw_text'][:60]}")
            set_zone_enabled(zone_name, False, f"команда оператора: {cmd['raw_text'][:60]}")
            logger.info("[Zones] Зона '%s' заморожена по команде оператора", zone_name)

        elif action in ("unfreeze_zone", "enable_zone"):
            set_policy(f"freeze_zone_{zone_name}", False, command_id=cmd["id"])
            set_zone_enabled(zone_name, True, f"команда оператора: {cmd['raw_text'][:60]}")
            logger.info("[Zones] Зона '%s' разморожена по команде оператора", zone_name)

        mark_command_applied(cmd["id"])
        applied += 1

    return applied


def get_zones_summary() -> str:
    """Возвращает краткую строку для Telegram-сводки."""
    zones = get_all_zones()
    lines = []
    icons = {True: "✅", False: "⛔"}
    for name in ("scheduling", "visual", "prelend", "code"):
        z = zones.get(name, {})
        enabled = bool(z.get("enabled"))
        score   = z.get("confidence_score", 0)
        frozen  = is_zone_frozen(name)
        frozen_tag = " 🔒" if frozen else ""
        lines.append(f"{icons[enabled]} {name}: {score}/100{frozen_tag}")
    return "\n".join(lines)
