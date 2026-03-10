"""
commander/notifier.py — Суточные сводки и уведомления в Telegram.

Отправляет сообщения только через Telegram Bot API (requests, без PTB overhead).
Накапливает события в таблице notifications, раз в сутки формирует дайджест.

Экспортирует:
    send_message(text)          → отправить разовое сообщение
    log_notification(...)       → добавить в буфер дайджеста
    send_daily_digest_if_due()  → отправить сводку если пришло время
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, date
from typing import Optional

import requests

import config
from db.connection  import get_db
from db.zones       import get_all_zones
from db.commands    import is_zone_frozen

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Отправка сообщений
# ─────────────────────────────────────────────────────────────────────────────

def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """
    Отправляет сообщение в Telegram.
    Возвращает True при успехе.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.debug("[Notifier] Telegram не настроен — сообщение пропущено")
        return False

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id":    config.TELEGRAM_CHAT_ID,
            "text":       text[:4096],   # Telegram лимит
            "parse_mode": parse_mode,
        }, timeout=10)
        if resp.status_code != 200:
            logger.warning("[Notifier] Telegram вернул %d: %s", resp.status_code, resp.text[:100])
            return False
        return True
    except Exception as exc:
        logger.warning("[Notifier] Ошибка отправки: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Буфер уведомлений
# ─────────────────────────────────────────────────────────────────────────────

def log_notification(
    message:  str,
    level:    str = "info",    # 'info' | 'warning' | 'error'
    category: str = "general", # 'plan' | 'zone' | 'patch' | 'rollback' | 'metric'
) -> None:
    """Записывает событие в буфер для суточного дайджеста."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO notifications (level, category, message) VALUES (?, ?, ?)",
            (level, category, message[:500])
        )


# ─────────────────────────────────────────────────────────────────────────────
# Суточный дайджест
# ─────────────────────────────────────────────────────────────────────────────

def send_daily_digest_if_due() -> bool:
    """
    Проверяет, пора ли отправлять суточную сводку (DAILY_DIGEST_TIME).
    Если да — формирует и отправляет.

    Защита от дублирования: проверяем, была ли уже отправлена сводка сегодня.
    """
    now      = datetime.now()
    today    = date.today().isoformat()

    # Проверяем время
    target_h, target_m = map(int, config.DAILY_DIGEST_TIME.split(":"))
    if not (now.hour == target_h and now.minute == target_m):
        return False

    # Проверяем, не отправляли ли сегодня
    with get_db() as conn:
        already_sent = conn.execute(
            "SELECT 1 FROM notifications WHERE digest_date = ? AND included_in_digest = 1 LIMIT 1",
            (today,)
        ).fetchone()

    if already_sent:
        return False

    # Формируем дайджест
    digest_text = _build_digest(today)
    if not digest_text:
        return False

    sent = send_message(digest_text)
    if sent:
        # Помечаем уведомления как включённые в сводку
        with get_db() as conn:
            conn.execute(
                """UPDATE notifications SET included_in_digest = 1, digest_date = ?
                   WHERE included_in_digest = 0""",
                (today,)
            )
        logger.info("[Notifier] Суточный дайджест отправлен (%s)", today)

    return sent


def _build_digest(today: str) -> str:
    """Формирует текст суточного дайджеста из данных в БД."""
    with get_db() as conn:
        # Статистика планов за сегодня
        plan_stats = conn.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'applied' THEN 1 ELSE 0 END) AS applied,
                SUM(CASE WHEN status = 'failed'  THEN 1 ELSE 0 END) AS failed
            FROM evolution_plans
            WHERE DATE(created_at) = ?
        """, (today,)).fetchone()

        # Статистика патчей за сегодня
        patch_stats = conn.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN rolled_back = 0 AND test_status = 'passed' THEN 1 ELSE 0 END) AS passed,
                SUM(CASE WHEN rolled_back = 1 THEN 1 ELSE 0 END) AS rolled_back
            FROM applied_changes
            WHERE change_type = 'code_patch' AND DATE(applied_at) = ?
        """, (today,)).fetchone()

        # Уведомления за сегодня (не включённые ещё в дайджест)
        notifications = conn.execute("""
            SELECT level, category, message
            FROM notifications
            WHERE included_in_digest = 0
            ORDER BY created_at DESC
            LIMIT 10
        """).fetchall()

    # Состояние зон
    zones        = get_all_zones()
    zones_lines  = []
    for name in ("scheduling", "visual", "prelend", "code"):
        z       = zones.get(name, {})
        enabled = bool(z.get("enabled"))
        score   = z.get("confidence_score", 0)
        icon    = "✅" if enabled else "⛔"
        zones_lines.append(f"  {icon} {name}: {score}/100")

    lines = [
        f"📊 <b>Orchestrator — Суточная сводка {today}</b>\n",
        "<b>Планы эволюции:</b>",
        f"  Создано: {plan_stats['total'] if plan_stats else 0}",
        f"  Применено: {plan_stats['applied'] if plan_stats else 0}",
        f"  Ошибок: {plan_stats['failed'] if plan_stats else 0}",
    ]

    if patch_stats and patch_stats["total"] > 0:
        lines += [
            "\n<b>Патчи кода:</b>",
            f"  Успешных: {patch_stats['passed']}",
            f"  Откатов: {patch_stats['rolled_back']}",
        ]

    lines += ["\n<b>Зоны доверия:</b>"] + zones_lines

    if notifications:
        lines.append("\n<b>События:</b>")
        for n in notifications:
            icon = {"error": "🔴", "warning": "🟡", "info": "🔵"}.get(n["level"], "⚪")
            lines.append(f"  {icon} [{n['category']}] {n['message'][:100]}")

    return "\n".join(lines)
