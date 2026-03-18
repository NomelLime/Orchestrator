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
    /proxies        → прокси и ожидающие запросы
    /patches        → список ожидающих патчей кода
    /approve_N      → одобрить патч #N
    /reject_N       → отклонить патч #N
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


# ─────────────────────────────────────────────────────────────────────────────
# Авторизация
# ─────────────────────────────────────────────────────────────────────────────

def _is_authorized(update) -> bool:
    """
    Проверяет что входящее сообщение от авторизованного chat_id.
    Если ORC_TG_CHAT_ID не задан — режим разработки, пропускаем всех.
    """
    if not config.TELEGRAM_CHAT_ID:
        return True
    try:
        return str(update.effective_chat.id) == str(config.TELEGRAM_CHAT_ID)
    except Exception:
        return False


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
    """
    Текстовое сообщение:
      'да <id>' / 'нет <id>' → подтверждение/отклонение proxy_event
      Всё остальное → сохраняется в operator_commands
    """
    if not _is_authorized(update):
        return
    from modules.supply_tracker import confirm_purchase, reject_purchase, get_pending_purchase

    text  = (update.message.text or "").strip()
    lower = text.lower()
    if not text:
        return

    # ── Подтверждение прокси-запроса ──────────────────────────────────────────
    if lower.startswith(("да", "yes", "нет", "no")):
        is_yes = lower.startswith(("да", "yes"))
        parts  = lower.split()

        # ID события — второй токен если числовой
        event_id: Optional[int] = None
        for part in parts[1:]:
            if part.isdigit():
                event_id = int(part)
                break

        # Если ID не указан — берём первый ожидающий запрос
        if event_id is None:
            pending = get_pending_purchase()
            if pending:
                event_id = pending["id"]

        if event_id is not None:
            pending = get_pending_purchase()
            if pending and pending["id"] == event_id:
                if is_yes:
                    ok = confirm_purchase(event_id)
                    if not ok:
                        await update.message.reply_text(
                            f"❌ Не удалось выполнить event #{event_id}. Проверь баланс."
                        )
                else:
                    reject_purchase(event_id)
                return
            else:
                await update.message.reply_text(
                    f"⚠️ Event #{event_id} не найден или уже обработан."
                )
                return

    # ── Обычная команда оператора ─────────────────────────────────────────────
    cmd_id = save_command(raw_text=text)
    await update.message.reply_text(
        f"✅ Команда принята (#{cmd_id}). Будет обработана на следующем цикле."
    )
    logger.info("[TelegramBot] Новая команда #%d: %s", cmd_id, text[:60])


async def _handle_zones(update, context) -> None:
    """/zones — состояние зон доверия."""
    if not _is_authorized(update):
        return
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
    if not _is_authorized(update):
        return
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
    if not _is_authorized(update):
        return
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


async def _handle_proxies(update, context) -> None:
    """/proxies — список прокси и ожидающие запросы."""
    if not _is_authorized(update):
        return
    from integrations.proxy_manager import get_my_proxies, get_balance
    from modules.supply_tracker import get_pending_purchase

    lines = ["🌐 <b>Прокси (mobileproxy.space)</b>\n"]

    balance = get_balance()
    lines.append(f"Баланс: <b>{balance:.0f} руб.</b>" if balance is not None else "Баланс: недоступен")

    proxies = get_my_proxies()
    if proxies:
        lines.append(f"\nАктивных прокси: {len(proxies)}")
        for p in proxies[:5]:  # показываем первые 5
            pid = p.get("proxy_id", "?")
            geo = p.get("proxy_geo", "?")
            exp = (p.get("proxy_exp") or "?")[:10]
            lines.append(f"  • #{pid} {geo} (до {exp})")
        if len(proxies) > 5:
            lines.append(f"  ... и ещё {len(proxies) - 5}")
    else:
        lines.append("\nПрокси не найдены (проверь ORC_MOBILEPROXY_API_KEY)")

    pending = get_pending_purchase()
    if pending:
        lines.append(
            f"\n⏳ <b>Ожидает подтверждения (event #{pending['id']}):</b>\n"
            f"  {pending.get('reason', '?')}\n"
            f"  Ответь: <b>да {pending['id']}</b> или <b>нет {pending['id']}</b>"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def _handle_patches(update, context) -> None:
    """/patches — список ожидающих патчей кода."""
    if not _is_authorized(update):
        return
    from db.patches import get_pending_patches
    patches = get_pending_patches()

    if not patches:
        await update.message.reply_text("📋 Нет ожидающих патчей кода.")
        return

    lines = [f"📋 <b>Ожидают одобрения: {len(patches)} патч(ей)</b>\n"]
    for p in patches:
        lines.append(
            f"  <b>#{p['id']}</b> — <code>{p['file_path']}</code>\n"
            f"  🎯 {p['goal'][:80]}\n"
            f"  План #{p['plan_id']} | {p['created_at'][:16]}\n"
            f"  ✅ /approve_{p['id']}  ❌ /reject_{p['id']}\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def _handle_approve(update, context) -> None:
    """/approve_N — одобрить патч #N."""
    if not _is_authorized(update):
        return
    from db.patches import mark_patch_approved, get_patch
    text = (update.message.text or "").strip()

    # Извлекаем ID: /approve_7 → 7
    try:
        patch_id = int(text.split("_", 1)[1].split()[0])
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Использование: /approve_7 (укажи номер патча)")
        return

    patch = get_patch(patch_id)
    if not patch:
        await update.message.reply_text(f"⚠️ Патч #{patch_id} не найден.")
        return

    ok = mark_patch_approved(patch_id)
    if ok:
        await update.message.reply_text(
            f"✅ <b>Патч #{patch_id} одобрен</b>\n"
            f"Файл: <code>{patch['file_path']}</code>\n"
            f"Будет применён в следующем цикле Orchestrator.",
            parse_mode="HTML"
        )
        logger.info("[TelegramBot] Оператор одобрил патч #%d: %s", patch_id, patch['file_path'])
    else:
        await update.message.reply_text(
            f"⚠️ Патч #{patch_id} не удалось одобрить "
            f"(статус: {patch.get('status', '?')} — возможно уже обработан)."
        )


async def _handle_reject(update, context) -> None:
    """/reject_N — отклонить патч #N."""
    if not _is_authorized(update):
        return
    from db.patches import mark_patch_rejected, get_patch
    text = (update.message.text or "").strip()

    # Извлекаем ID: /reject_7 → 7
    try:
        patch_id = int(text.split("_", 1)[1].split()[0])
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Использование: /reject_7 (укажи номер патча)")
        return

    patch = get_patch(patch_id)
    if not patch:
        await update.message.reply_text(f"⚠️ Патч #{patch_id} не найден.")
        return

    ok = mark_patch_rejected(patch_id)
    if ok:
        await update.message.reply_text(
            f"❌ <b>Патч #{patch_id} отклонён</b>\n"
            f"Файл: <code>{patch['file_path']}</code>\n"
            f"Код не будет изменён.",
            parse_mode="HTML"
        )
        logger.info("[TelegramBot] Оператор отклонил патч #%d: %s", patch_id, patch['file_path'])
    else:
        await update.message.reply_text(
            f"⚠️ Патч #{patch_id} не удалось отклонить "
            f"(статус: {patch.get('status', '?')} — возможно уже обработан)."
        )


async def _handle_help(update, context) -> None:
    """/help — справка."""
    if not _is_authorized(update):
        return
    await update.message.reply_text(
        "🤖 <b>Orchestrator COMMANDER</b>\n\n"
        "<b>Команды:</b>\n"
        "/zones — состояние зон доверия\n"
        "/last_plan — последний план эволюции\n"
        "/status — статус системы\n"
        "/proxies — прокси и ожидающие запросы\n"
        "/patches — ожидающие патчи кода\n"
        "/approve_N — одобрить патч #N\n"
        "/reject_N — отклонить патч #N\n"
        "/freeze — отменить текущий ожидающий план\n"
        "/cancel_plan — то же что /freeze (синоним)\n"
        "/trigger — запустить внеочередной цикл немедленно\n"
        "/help — эта справка\n\n"
        "<b>Подтверждение прокси:</b>\n"
        "  да 7 — выполнить запрос #7\n"
        "  нет 7 — отменить запрос #7\n\n"
        "<b>Свободный текст:</b>\n"
        "Любое сообщение будет интерпретировано и применено.\n"
        "Примеры:\n"
        "  «заморозь зону visual»\n"
        "  «фокус на GEO Бразилия»\n"
        "  «поставь режим safe»\n"
        "  «откати последний план»",
        parse_mode="HTML"
    )


async def _handle_freeze(update, context) -> None:
    """/freeze — мгновенная отмена ожидающего плана."""
    if not _is_authorized(update):
        return
    from main_orchestrator import cancel_pending_plan
    cancel_pending_plan()
    await update.message.reply_text(
        "🛑 Сигнал отмены плана отправлен.\n"
        "Если план был в ожидании — он будет помечен failed.",
        parse_mode="HTML"
    )


async def _handle_cancel_plan(update, context) -> None:
    """/cancel_plan — синоним /freeze."""
    await _handle_freeze(update, context)


async def _handle_trigger(update, context) -> None:
    """/trigger — запустить внеочередной цикл немедленно."""
    if not _is_authorized(update):
        return
    from main_orchestrator import trigger_force_cycle
    trigger_force_cycle()
    await update.message.reply_text("⚡ Внеочередной цикл запущен.")


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

    app.add_handler(CommandHandler("zones",       _handle_zones))
    app.add_handler(CommandHandler("last_plan",   _handle_last_plan))
    app.add_handler(CommandHandler("status",      _handle_status))
    app.add_handler(CommandHandler("proxies",     _handle_proxies))
    app.add_handler(CommandHandler("patches",     _handle_patches))
    app.add_handler(CommandHandler("freeze",      _handle_freeze))
    app.add_handler(CommandHandler("cancel_plan", _handle_cancel_plan))
    app.add_handler(CommandHandler("trigger",     _handle_trigger))
    app.add_handler(CommandHandler("help",        _handle_help))
    # /approve_N и /reject_N — динамические команды с суффиксом ID
    # python-telegram-bot CommandHandler принимает команды с аргументами,
    # но /approve_7 парсится как команда "approve_7" (без аргументов).
    # Используем filters.Regex для перехвата паттерна /approve_\d+ и /reject_\d+
    app.add_handler(MessageHandler(
        filters.COMMAND & filters.Regex(r"^/approve_\d+"),
        _handle_approve,
    ))
    app.add_handler(MessageHandler(
        filters.COMMAND & filters.Regex(r"^/reject_\d+"),
        _handle_reject,
    ))
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
