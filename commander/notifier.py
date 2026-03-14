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
from typing import Any, Dict, Optional

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

    # Проверяем время — сравниваем только часы (цикл почасовой, точный минутный матч ненадёжен)
    target_h = int(config.DAILY_DIGEST_TIME.split(":")[0])
    if now.hour != target_h:
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

        # Последние снапшоты метрик
        sp_snap = conn.execute("""
            SELECT sp_total_views, sp_total_likes, sp_avg_ctr,
                   sp_top_platform, sp_ban_count
            FROM metrics_snapshots
            WHERE source = 'ShortsProject'
            ORDER BY snapshot_at DESC LIMIT 1
        """).fetchone()

        pl_snap = conn.execute("""
            SELECT pl_total_clicks, pl_conversions, pl_cr,
                   pl_bot_pct, pl_top_geo, raw_summary_json
            FROM metrics_snapshots
            WHERE source = 'PreLend'
            ORDER BY snapshot_at DESC LIMIT 1
        """).fetchone()

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
        frozen  = is_zone_frozen(name)
        icon    = "🔒" if frozen else ("✅" if enabled else "⛔")
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

    # ── Метрики ShortsProject ─────────────────────────────────────────────────
    if sp_snap and sp_snap["sp_total_views"] is not None:
        ctr_str = f"{sp_snap['sp_avg_ctr']:.3f}" if sp_snap["sp_avg_ctr"] else "—"
        lines += [
            "\n<b>ShortsProject (последний снапшот):</b>",
            f"  👁 Просмотры:   {sp_snap['sp_total_views']:,}",
            f"  👍 Лайки:       {sp_snap['sp_total_likes'] or 0:,}",
            f"  📈 CTR:         {ctr_str}",
            f"  🏆 Топ:         {sp_snap['sp_top_platform'] or '—'}",
            f"  🚫 Бан-события: {sp_snap['sp_ban_count'] or 0}",
        ]

    # ── Метрики PreLend ───────────────────────────────────────────────────────
    if pl_snap and pl_snap["pl_total_clicks"] is not None:
        bot_pct_str = f"{pl_snap['pl_bot_pct']:.1f}%" if pl_snap["pl_bot_pct"] is not None else "—"
        cr_str      = f"{pl_snap['pl_cr']:.4f}"       if pl_snap["pl_cr"]       else "—"
        lines += [
            "\n<b>PreLend (последний снапшот):</b>",
            f"  🖱 Кликов:     {pl_snap['pl_total_clicks']:,}",
            f"  💰 Конверсий:  {pl_snap['pl_conversions'] or 0}",
            f"  📊 CR:         {cr_str}",
            f"  🤖 Ботов:      {bot_pct_str}",
            f"  🌍 Топ ГЕО:    {pl_snap['pl_top_geo'] or '—'}",
        ]
        # shave_suspects живёт в raw_summary_json
        if pl_snap["raw_summary_json"]:
            try:
                raw = json.loads(pl_snap["raw_summary_json"])
                suspects = raw.get("shave_suspects", [])
                if suspects:
                    lines.append(f"  ⚠️ Шейв:       {suspects}")
            except Exception:
                pass

    if notifications:
        lines.append("\n<b>События:</b>")
        for n in notifications:
            icon = {"error": "🔴", "warning": "🟡", "info": "🔵"}.get(n["level"], "⚪")
            lines.append(f"  {icon} [{n['category']}] {n['message'][:100]}")

    return "\n".join(lines)
