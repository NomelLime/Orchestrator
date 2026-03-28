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
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import portalocker

import config
import startup_check
from db.connection   import init_db
from modules         import orchestrator_graph
from commander       import notifier
from commander       import telegram_bot
from db.commands     import get_policy

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

_log_file = config.BASE_DIR / "data" / "orchestrator.log"
if os.getenv("LOG_FORMAT", "").strip().lower() == "json":
    try:
        from pythonjsonlogger import jsonlogger

        _root = logging.getLogger()
        _root.handlers.clear()
        _root.setLevel(logging.INFO)
        _fmt = jsonlogger.JsonFormatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        _sh = logging.StreamHandler()
        _sh.setFormatter(_fmt)
        _fh = logging.FileHandler(_log_file, encoding="utf-8")
        _fh.setFormatter(_fmt)
        _root.addHandler(_sh)
        _root.addHandler(_fh)
    except Exception:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(_log_file, encoding="utf-8"),
            ],
        )
else:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(_log_file, encoding="utf-8"),
        ],
    )
logger = logging.getLogger("Orchestrator")


# ─────────────────────────────────────────────────────────────────────────────
# Главный цикл
# ─────────────────────────────────────────────────────────────────────────────

def run_cycle(cycle_num: int = 0) -> None:
    """Один полный цикл Orchestrator через LangGraph."""
    cycle_start = datetime.now()
    logger.info("=" * 60)
    logger.info("Цикл #%d начат: %s | DRY_RUN=%s", cycle_num, cycle_start.isoformat(), config.DRY_RUN)

    try:
        orchestrator_graph.run_cycle_graph(cycle_num=cycle_num)

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

    # Zone 2 (visual): поднимаем score 50 → 70 и включаем зону (Сессия 11)
    # Выполняется только если score ещё не поднимался (идемпотентно).
    try:
        from db.connection import get_db as _get_db_main
        with _get_db_main() as _conn:
            _conn.execute(
                "UPDATE zones SET confidence_score = 70, enabled = 1 "
                "WHERE zone_name = 'visual' AND confidence_score <= 50"
            )
        logger.info("[Orchestrator] Zone 2 (visual): score → 70, enabled=1")
    except Exception as _ze:
        logger.warning("[Orchestrator] Zone 2 migration не удалась: %s", _ze)

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
