"""
main_orchestrator.py — Главный цикл Orchestrator.

Запуск: python main_orchestrator.py
        ORC_DRY_RUN=true python main_orchestrator.py  (безопасный режим)

Цикл (раз в CYCLE_INTERVAL_HOURS):
    0. Ретроспективная оценка изменений за 24ч (evaluator)
    1. Пассивная деградация зон (confidence_score decay)
    2. Обработка команд оператора из Telegram (pending → applied)
    3. Сбор метрик из ShortsProject и PreLend → metrics_snapshots
    3.5 Проверка краш-лупа агентов SP → автооткат последнего патча
    3.6 SP Pipeline manager: запуск run_pipeline.py если очередь низкая
    3.7 Проверка прокси/баланса (раз в SUPPLY_CHECK_EVERY_N_CYCLES циклов)
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
import threading
import time
from datetime import datetime
from pathlib import Path

import portalocker

import config
import startup_check
from db.connection   import init_db
from modules         import tracking, zones as zones_module, evolution, policies
from modules         import config_enforcer, code_evolver, evaluator, supply_tracker, sp_runner
from commander       import notifier
from commander       import telegram_bot
from db.experiences  import mark_plan_applied, mark_plan_failed
from db.commands     import get_policy, cleanup_expired_policies

# ─────────────────────────────────────────────────────────────────────────────
# События для межпоточного управления циклом
# ─────────────────────────────────────────────────────────────────────────────

# Устанавливается telegram_bot при /freeze или /cancel_plan → прерывает ожидание плана
_cancel_plan: threading.Event = threading.Event()

# Устанавливается telegram_bot при /trigger → запускает внеочередной цикл
_force_cycle: threading.Event = threading.Event()


def cancel_pending_plan() -> None:
    """Вызывается из telegram_bot для мгновенной отмены ожидающего плана."""
    _cancel_plan.set()


def trigger_force_cycle() -> None:
    """Вызывается из telegram_bot для запуска внеочередного цикла без ожидания."""
    _force_cycle.set()

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

def run_cycle(cycle_num: int = 0) -> None:
    """Один полный цикл Orchestrator. cycle_num используется для throttle-логики."""
    cycle_start = datetime.now()
    logger.info("=" * 60)
    logger.info("Цикл #%d начат: %s | DRY_RUN=%s", cycle_num, cycle_start.isoformat(), config.DRY_RUN)

    try:
        # ── Шаг 0: Ретроспективная оценка изменений (24h delayed) ────────────
        evaluated = evaluator.evaluate_pending_changes()
        if evaluated:
            logger.info("[0/7] Ретроспективная оценка: %d изменений оценено", evaluated)

        # ── Шаг 0.5: Очистка истекших политик ──────────────────────────────
        cleanup_expired_policies()

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

        # ── Шаг 3.5: Краш-луп детектор ───────────────────────────────────────
        reverted = code_evolver.check_and_revert_on_crash()

        if reverted:
            logger.warning("[3.5/7] Краш-луп: автооткат выполнен — пропускаем генерацию плана")
            return

        # ── Шаг 3.6: SP Pipeline manager ─────────────────────────────────────
        logger.info("[3.6/8] SP Pipeline manager...")
        sp_runner.manage_sp_pipeline(metrics_data)

        # ── Шаг 3.7: Мониторинг прокси (раз в N циклов) ──────────────────────
        if cycle_num % config.SUPPLY_CHECK_EVERY_N_CYCLES == 0:
            logger.info("[3.7/8] Проверка прокси/баланса...")
            supply_requests = supply_tracker.check_supply(sp)
            if supply_requests:
                logger.info("  Отправлено запросов оператору: %d", supply_requests)

        # ── Шаг 4: Генерация плана ───────────────────────────────────────────
        logger.info("[4/8] Генерация плана эволюции (LLM)...")
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

        # Пауза перед применением (если задана) — прерываемая через /freeze или /cancel_plan
        if config.PLAN_APPLY_DELAY_SEC > 0:
            delay_min = config.PLAN_APPLY_DELAY_SEC // 60
            notifier.send_message(
                f"⏳ <b>Применение плана #{plan_id} через {delay_min} мин.</b>\n"
                f"Отправьте /freeze или /cancel_plan чтобы отменить."
            )
            logger.info("  Ожидание %d сек перед применением...", config.PLAN_APPLY_DELAY_SEC)
            _cancel_plan.clear()
            cancelled = _cancel_plan.wait(timeout=config.PLAN_APPLY_DELAY_SEC)

            # После ожидания — явная проверка отмены
            if cancelled or get_policy("pause_evolution"):
                mark_plan_failed(plan_id)
                notifier.send_message(f"🛑 <b>План #{plan_id} отменён оператором</b>")
                logger.warning("[Orchestrator] План #%d отменён во время ожидания", plan_id)
                return

        # ── Шаг 5: Применение конфиг-изменений ───────────────────────────────
        logger.info("[5/8] Применение config_changes...")
        cfg_ok, cfg_fail = config_enforcer.apply_config_changes(plan, plan_id)
        logger.info("  Конфиги: успешно=%d, ошибок=%d", cfg_ok, cfg_fail)
        if cfg_ok or cfg_fail:
            notifier.log_notification(
                f"Config changes план #{plan_id}: +{cfg_ok} ошибок:{cfg_fail}",
                category="patch",
                level="info" if cfg_fail == 0 else "warning"
            )

        # ── Шаг 6: Патчи кода (двухшаговый: одобрение → применение) ─────────
        logger.info("[6/8] Code patches (Zone 4)...")

        # 6a. Применяем ранее одобренные патчи (из прошлых циклов)
        code_ok, code_fail = code_evolver.apply_approved_patches()
        if code_ok or code_fail:
            logger.info("  Одобренные патчи применены: успешно=%d, откатов=%d", code_ok, code_fail)
            notifier.log_notification(
                f"Code patches применены: +{code_ok} откатов:{code_fail}",
                category="patch",
                level="info" if code_fail == 0 else "warning"
            )

        # 6b. Ставим в очередь новые патчи из текущего плана → уведомление в Telegram
        queued = code_evolver.queue_code_patches(plan, plan_id)
        if queued:
            logger.info("  Новых патчей поставлено в очередь: %d (ожидают /approve_N)", queued)
            notifier.log_notification(
                f"Plan #{plan_id}: {queued} патч(ей) ожидают одобрения в Telegram",
                category="patch",
                level="info"
            )

        # Помечаем план: считаем config_changes + применённые code_patches
        # queued-патчи не считаются применёнными — они ожидают одобрения
        total_ok   = cfg_ok + code_ok
        total_fail = cfg_fail + code_fail
        if total_ok > 0:
            mark_plan_applied(plan_id)
        elif total_fail > 0:
            mark_plan_failed(plan_id)
        # Если только queued (без cfg_ok/code_ok) — план остаётся pending до применения

        # ── Шаг 7: Суточный дайджест ─────────────────────────────────────────
        logger.info("[7/8] Проверка суточного дайджеста...")
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

    # Проверка зависимостей — при критических ошибках выходим сразу
    startup_check.run_checks(abort_on_fail=True)

    # Инициализация БД (идемпотентно — безопасно запускать повторно)
    init_db()

    # Запуск Telegram-бота в фоновом потоке
    telegram_bot.start_bot_thread()

    cycle_interval_sec = config.CYCLE_INTERVAL_HOURS * 3600
    cycle_num = 0

    while True:
        # Защита от перекрытия циклов через файловую блокировку
        try:
            lock_file = open(str(config.CYCLE_LOCK_FILE), "w")
            portalocker.lock(lock_file, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.AlreadyLocked:
            lock_file.close()
            logger.warning("[Orchestrator] Другой цикл ещё выполняется — пропускаем")
            time.sleep(60)
            continue

        try:
            run_cycle(cycle_num=cycle_num)
        finally:
            portalocker.unlock(lock_file)
            lock_file.close()

        cycle_num += 1

        # Проверяем force_cycle ДО ожидания (мог быть установлен в конце предыдущего цикла)
        if get_policy("force_cycle"):
            from db.commands import set_policy as _set_policy
            _set_policy("force_cycle", False)
            logger.info("Внеочередной цикл (force_cycle из прошлого цикла)")
            continue

        logger.info("Следующий цикл через %d часов", config.CYCLE_INTERVAL_HOURS)
        _force_cycle.clear()
        triggered = _force_cycle.wait(timeout=cycle_interval_sec)
        if triggered:
            logger.info("Внеочередной цикл по запросу оператора (/trigger)")


if __name__ == "__main__":
    main()
