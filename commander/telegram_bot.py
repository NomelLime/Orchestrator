"""
commander/telegram_bot.py — Telegram-бот Orchestrator COMMANDER.

Отдельный бот (не тот что у ShortsProject/PreLend — разные токены!).
Принимает свободный текст → сохраняет в operator_commands как pending.
Интерпретация через LLM происходит в modules/policies.py в основном цикле.

Запуск: python -m commander.telegram_bot (отдельный процесс)
Или вызывать run_bot() из main_orchestrator.py в фоновом потоке.

Поддерживаемые команды:
    Свободный текст → operator_commands (pending)

    /zones          → показать состояние зон
    /last_plan      → последний план эволюции
    /status         → статус системы
    /help           → справка

Экспортирует:
    run_bot()       → запускает polling (блокирующий вызов)
    start_bot_thread() → запускает бот в фоновом потоке
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import config
from db.commands   import save_command
from db.zones      import get_all_zones
from db.experiences import get_recent_experience
from db.metrics    import get_latest_snapshot
from db.commands   import is_zone_frozen

logger = logging.getLogger(__name__)


def _get_application():
    """Создаёт telegram Application. Вынесено для ленивого импорта."""
    try:
        from telegram.ext import Application, MessageHandler, CommandHandler, filters
        return Application, MessageHandler, CommandHandler, filters
    except ImportError:
        raise RuntimeError(
            "python-telegram-bot не установлен. pip install python-telegram-bot"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────────────────────────────────────

async def _handle_text(update, context) -> None:
    """Любое текстовое сообщение → сохранить в operator_commands."""
    text = update.message.text or ""
    if not text.strip():
        return

    cmd_id = save_command(raw_text=text)
    await update.message.reply_text(
        f"✅ Команда принята (#{cmd_id}). Будет обработана на следующем цикле."
    )
    logger.info("[TelegramBot] Новая команда #%d: %s", cmd_id, text[:60])


async def _handle_zones(update, context) -> None:
    """/zones — состояние зон доверия."""
    zones = get_all_zones()
    lines = ["📊 <b>Зоны доверия Orchestrator:</b>\n"]
    icons = {True: "✅", False: "⛔"}

    for name in ("scheduling", "visual", "prelend", "code"):
        z       = zones.get(name, {})
        enabled = bool(z.get("enabled"))
        score   = z.get("confidence_score", 0)
        frozen  = is_zone_frozen(name)
        tag     = " 🔒заморожена" if frozen else ""
        lines.append(f"{icons[enabled]} <b>{name}</b>: {score}/100{tag}")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML"
    )


async def _handle_last_plan(update, context) -> None:
    """/last_plan — последний план эволюции."""
    from db.connection import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM evolution_plans ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

    if not row:
        await update.message.reply_text("📋 Планов ещё нет.")
        return

    await update.message.reply_text(
        f"📋 <b>Последний план #{row['id']}</b>\n"
        f"Создан: {row['created_at']}\n"
        f"Статус: {row['status']}\n"
        f"Риск: {row['risk_level']}\n\n"
        f"{row['summary']}",
        parse_mode="HTML"
    )


async def _handle_status(update, context) -> None:
    """/status — общий статус системы."""
    sp_snap = get_latest_snapshot("ShortsProject")
    pl_snap = get_latest_snapshot("PreLend")

    from integrations.ollama_client import is_ollama_available
    ollama_ok = is_ollama_available()

    lines = [
        "🤖 <b>Orchestrator Status</b>\n",
        f"Ollama: {'✅ доступна' if ollama_ok else '❌ недоступна'}",
    ]

    if sp_snap:
        lines.append(
            f"\n📱 <b>ShortsProject</b> ({sp_snap['snapshot_at'][:16]}):\n"
            f"  Views: {sp_snap.get('sp_total_views') or 0:,}\n"
            f"  Bans: {sp_snap.get('sp_ban_count') or 0}"
        )

    if pl_snap:
        cr  = pl_snap.get("pl_cr")
        bot = pl_snap.get("pl_bot_pct")
        lines.append(
            f"\n🌐 <b>PreLend</b> ({pl_snap['snapshot_at'][:16]}):\n"
            f"  Clicks: {pl_snap.get('pl_total_clicks') or 0:,}\n"
            f"  CR: {f'{cr:.3f}' if cr else 'н/д'}\n"
            f"  Bots: {f'{bot:.1f}%' if bot is not None else 'н/д'}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def _handle_help(update, context) -> None:
    """/help — справка."""
    await update.message.reply_text(
        "🤖 <b>Orchestrator COMMANDER</b>\n\n"
        "<b>Команды:</b>\n"
        "/zones — состояние зон доверия\n"
        "/last_plan — последний план эволюции\n"
        "/status — статус системы\n"
        "/help — эта справка\n\n"
        "<b>Свободный текст:</b>\n"
        "Любое сообщение будет интерпретировано и применено.\n"
        "Примеры:\n"
        "  «заморозь зону visual»\n"
        "  «фокус на GEO Бразилия»\n"
        "  «поставь режим safe»\n"
        "  «откати последний план»",
        parse_mode="HTML"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Запуск бота
# ─────────────────────────────────────────────────────────────────────────────

def run_bot() -> None:
    """
    Запускает Telegram-бот в polling-режиме.
    Блокирующий вызов — используй start_bot_thread() для фонового запуска.
    """
    if not config.TELEGRAM_BOT_TOKEN:
        logger.warning("[TelegramBot] ORC_TG_TOKEN не задан — бот не запущен")
        return

    Application, MessageHandler, CommandHandler, filters = _get_application()

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("zones",     _handle_zones))
    app.add_handler(CommandHandler("last_plan", _handle_last_plan))
    app.add_handler(CommandHandler("status",    _handle_status))
    app.add_handler(CommandHandler("help",      _handle_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text))

    logger.info("[TelegramBot] Запуск polling...")
    app.run_polling(drop_pending_updates=True)


def start_bot_thread() -> Optional[threading.Thread]:
    """Запускает бот в фоновом daemon-потоке. Возвращает Thread или None."""
    if not config.TELEGRAM_BOT_TOKEN:
        logger.warning("[TelegramBot] ORC_TG_TOKEN не задан — бот не запущен")
        return None

    t = threading.Thread(target=run_bot, daemon=True, name="OrchestratorBot")
    t.start()
    logger.info("[TelegramBot] Фоновый поток запущен")
    return t
