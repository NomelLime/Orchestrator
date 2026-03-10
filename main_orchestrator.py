"""
main_orchestrator.py — Главный цикл Orchestrator.

Запуск: python main_orchestrator.py
        ORC_DRY_RUN=true python main_orchestrator.py  (безопасный режим)

Цикл (раз в CYCLE_INTERVAL_HOURS):
    1. Пассивная деградация зон (confidence_score decay)
    2. Обработка команд оператора из Telegram (pending → applied)
    3. Сбор метрик из ShortsProject и PreLend → metrics_snapshots
    4. LLM-анализ + генерация плана → evolution_plans
    5. Применение config_changes (Zone 1, 2)
    6. Применение code_patches (Zone 4, только Python/SP)
    7. Суточный дайджест если пришло время
    8. Сон до следующего цикла

Защита от перекрытия циклов: portalocker на файле CYCLE_LOCK_FILE.
При DRY_RUN=true шаги 5 и 6 пропускаются.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

import portalocker

import config
from db.connection   import init_db
from modules         import tracking, zones as zones_module, evolution, policies
from modules         import config_enforcer, code_evolver
from commander       import notifier
from commander       import telegram_bot
from db.experiences  import mark_plan_applied, mark_plan_failed
from db.commands     import get_policy

# ─────────────────────────────────────────────────────────────────────────────
# Логирование
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
    handlers = [
        logging.StreamHandler(),
        logging.FileHandler(
            config.BASE_DIR / "data" / "orchestrator.log",
            encoding = "utf-8",
        ),
    ]
)
logger = logging.getLogger("Orchestrator")


# ─────────────────────────────────────────────────────────────────────────────
# Главный цикл
# ─────────────────────────────────────────────────────────────────────────────

def run_cycle() -> None:
    """Один полный цикл Orchestrator."""
    cycle_start = datetime.now()
    logger.info("=" * 60)
    logger.info("Цикл начат: %s | DRY_RUN=%s", cycle_start.isoformat(), config.DRY_RUN)

    try:
        # ── Шаг 1: Деградация зон ────────────────────────────────────────────
        logger.info("[1/7] Деградация зон...")
        zones_module.run_decay()

        # ── Шаг 2: Команды оператора ─────────────────────────────────────────
        logger.info("[2/7] Обработка команд оператора...")
        n_commands = policies.process_pending_commands()
        if n_commands:
            logger.info("  Обработано команд: %d", n_commands)

        # ── Проверка паузы эволюции ───────────────────────────────────────────
        if get_policy("pause_evolution"):
            logger.info("[Orchestrator] Эволюция на паузе (команда оператора) — цикл пропущен")
            return

        # ── Шаг 3: Сбор метрик ───────────────────────────────────────────────
        logger.info("[3/7] Сбор метрик...")
        metrics_data = tracking.collect_all_and_save()

        sp = metrics_data["shorts_project"]
        pl = metrics_data["prelend"]
        logger.info(
            "  SP: views=%d, bans=%d | PreLend: clicks=%d, CR=%s",
            sp["total_views"], sp["ban_count"],
            pl["total_clicks"], f"{pl['cr']:.4f}" if pl.get("cr") else "н/д",
        )

        # ── Шаг 4: Генерация плана ───────────────────────────────────────────
        logger.info("[4/7] Генерация плана эволюции (LLM)...")
        plan = evolution.generate_plan(metrics_data)

        if not plan:
            logger.warning("[Orchestrator] LLM не вернула план — цикл завершён без изменений")
            notifier.log_notification(
                "LLM не вернула план эволюции", level="warning", category="plan"
            )
            return

        plan_id = plan["_plan_id"]
        logger.info("  План #%d: %s (риск: %s)", plan_id, plan.get("summary", "")[:60],
                    plan.get("risk_assessment", {}).get("estimated_risk", "?"))

        # Отправляем план в Telegram (не ждём подтверждения — применяем автоматически)
        notifier.send_message(
            f"📋 <b>Orchestrator — Новый план #{plan_id}</b>\n"
            f"Риск: {plan.get('risk_assessment', {}).get('estimated_risk', '?')}\n\n"
            f"{plan.get('summary', '')}",
        )
        notifier.log_notification(
            f"Сгенерирован план #{plan_id}: {plan.get('summary', '')[:80]}",
            category="plan"
        )

        if config.DRY_RUN:
            logger.info("[Orchestrator] DRY_RUN — план не применяется")
            return

        # Пауза перед применением (если задана)
        if config.PLAN_APPLY_DELAY_SEC > 0:
            logger.info("  Ожидание %d сек перед применением...", config.PLAN_APPLY_DELAY_SEC)
            time.sleep(config.PLAN_APPLY_DELAY_SEC)

        # ── Шаг 5: Применение конфиг-изменений ───────────────────────────────
        logger.info("[5/7] Применение config_changes...")
        cfg_ok, cfg_fail = config_enforcer.apply_config_changes(plan, plan_id)
        logger.info("  Конфиги: успешно=%d, ошибок=%d", cfg_ok, cfg_fail)
        if cfg_ok or cfg_fail:
            notifier.log_notification(
                f"Config changes план #{plan_id}: +{cfg_ok} ошибок:{cfg_fail}",
                category="patch",
                level="info" if cfg_fail == 0 else "warning"
            )

        # ── Шаг 6: Применение патчей кода ────────────────────────────────────
        logger.info("[6/7] Применение code_patches (Zone 4)...")
        code_ok, code_fail = code_evolver.apply_code_patches(plan, plan_id)
        logger.info("  Патчи: успешно=%d, откатов=%d", code_ok, code_fail)
        if code_ok or code_fail:
            notifier.log_notification(
                f"Code patches план #{plan_id}: +{code_ok} откатов:{code_fail}",
                category="patch",
                level="info" if code_fail == 0 else "warning"
            )

        # Помечаем план
        total_ok   = cfg_ok + code_ok
        total_fail = cfg_fail + code_fail
        if total_ok > 0:
            mark_plan_applied(plan_id)
        elif total_fail > 0:
            mark_plan_failed(plan_id)

        # ── Шаг 7: Суточный дайджест ─────────────────────────────────────────
        logger.info("[7/7] Проверка суточного дайджеста...")
        notifier.send_daily_digest_if_due()

    except Exception as exc:
        logger.error("[Orchestrator] КРИТИЧЕСКАЯ ОШИБКА в цикле: %s", exc, exc_info=True)
        notifier.send_message(f"🔴 <b>Orchestrator ОШИБКА</b>\n{str(exc)[:500]}")

    finally:
        duration = (datetime.now() - cycle_start).total_seconds()
        logger.info("Цикл завершён за %.1f сек", duration)
        logger.info("=" * 60)


# ─────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    """Точка входа. Инициализирует БД, запускает Telegram-бот и основной цикл."""
    logger.info("Orchestrator запускается...")
    logger.info("DRY_RUN=%s | CYCLE=%dh | SP=%s | PL=%s",
                config.DRY_RUN,
                config.CYCLE_INTERVAL_HOURS,
                config.SHORTS_PROJECT_DIR,
                config.PRELEND_DIR)

    # Инициализация БД (идемпотентно — безопасно запускать повторно)
    init_db()

    # Запуск Telegram-бота в фоновом потоке
    telegram_bot.start_bot_thread()

    cycle_interval_sec = config.CYCLE_INTERVAL_HOURS * 3600

    while True:
        # Защита от перекрытия циклов через файловую блокировку
        try:
            lock_file = open(str(config.CYCLE_LOCK_FILE), "w")
            portalocker.lock(lock_file, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.AlreadyLocked:
            logger.warning("[Orchestrator] Другой цикл ещё выполняется — пропускаем")
            time.sleep(60)
            continue

        try:
            run_cycle()
        finally:
            portalocker.unlock(lock_file)
            lock_file.close()

        logger.info("Следующий цикл через %d часов", config.CYCLE_INTERVAL_HOURS)
        time.sleep(cycle_interval_sec)


if __name__ == "__main__":
    main()
