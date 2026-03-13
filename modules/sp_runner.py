"""
modules/sp_runner.py — Управление процессом ShortsProject pipeline.

Orchestrator запускает run_pipeline.py как дочерний subprocess и управляет
его жизненным циклом: запуск, мониторинг, детект зависания, алерт при краше.

Триггеры запуска (проверяются каждый цикл Orchestrator):
  1. upload_queue суммарно по всем аккаунтам < SP_PIPELINE_QUEUE_THRESHOLD
  2. С последнего запуска прошло > SP_PIPELINE_INTERVAL_HOURS

Состояние сохраняется в PID-файл — Orchestrator восстанавливает слежку
за процессом после перезапуска.

Экспортирует:
    manage_sp_pipeline(metrics_data) → None  (вызывается из main_orchestrator)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
from commander.notifier import send_message

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Внутреннее состояние (module-level singleton)
# ─────────────────────────────────────────────────────────────────────────────

_process: Optional[subprocess.Popen] = None
_started_at: Optional[float] = None   # unix timestamp старта текущего процесса
_last_finished_at: Optional[float] = None  # unix timestamp последнего завершения


# ─────────────────────────────────────────────────────────────────────────────
# PID-файл: персистентность состояния между перезапусками Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def _save_pid(pid: int, started_at: float) -> None:
    try:
        config.SP_PIPELINE_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.SP_PIPELINE_PID_FILE.write_text(
            json.dumps({"pid": pid, "started_at": started_at}),
            encoding="utf-8",
        )
    except Exception as e:
        logger.debug("[sp_runner] PID-файл не сохранён: %s", e)


def _load_pid() -> Optional[dict]:
    try:
        if config.SP_PIPELINE_PID_FILE.exists():
            return json.loads(config.SP_PIPELINE_PID_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _clear_pid() -> None:
    try:
        if config.SP_PIPELINE_PID_FILE.exists():
            config.SP_PIPELINE_PID_FILE.unlink()
    except Exception:
        pass


def _pid_is_alive(pid: int) -> bool:
    """Проверяет, живёт ли процесс с данным PID (кросс-платформенно)."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Управление процессом
# ─────────────────────────────────────────────────────────────────────────────

def is_running() -> bool:
    """Возвращает True если SP pipeline процесс сейчас активен."""
    global _process, _started_at

    if _process is not None:
        return _process.poll() is None  # None = ещё работает

    # Восстановление после перезапуска Orchestrator: проверяем PID-файл
    saved = _load_pid()
    if saved and _pid_is_alive(saved["pid"]):
        logger.info("[sp_runner] Восстановлен PID %d из файла (запущен %s)",
                    saved["pid"],
                    datetime.fromtimestamp(saved["started_at"]).strftime("%H:%M:%S"))
        _started_at = saved["started_at"]
        # Не можем получить Popen объект по PID — просто следим через os.kill
        # Используем флаг _started_at для детекта зависания
        return True

    _clear_pid()
    return False


def _count_queue_depth() -> int:
    """Подсчитывает суммарное число видео в upload_queue по всем аккаунтам."""
    total = 0
    video_ext = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
    accounts_dir = config.SP_ACCOUNTS_DIR
    if not accounts_dir.exists():
        return 0
    for acc_dir in accounts_dir.iterdir():
        queue_root = acc_dir / "upload_queue"
        if not queue_root.exists():
            continue
        for p in queue_root.rglob("*"):
            if p.suffix.lower() in video_ext and p.stat().st_size > 0:
                total += 1
    return total


def _should_trigger() -> tuple[bool, str]:
    """
    Определяет, нужно ли запускать pipeline прямо сейчас.
    Возвращает (True, причина) или (False, причина).
    """
    global _last_finished_at

    if is_running():
        return False, "уже запущен"

    # Проверка минимального интервала
    if _last_finished_at is not None:
        elapsed_h = (time.time() - _last_finished_at) / 3600
        if elapsed_h < config.SP_PIPELINE_INTERVAL_HOURS:
            remaining = config.SP_PIPELINE_INTERVAL_HOURS - elapsed_h
            return False, f"слишком рано (ещё {remaining:.1f}ч)"

    # Проверка глубины очереди
    depth = _count_queue_depth()
    if depth >= config.SP_PIPELINE_QUEUE_THRESHOLD:
        return False, f"очередь полная ({depth} видео ≥ порога {config.SP_PIPELINE_QUEUE_THRESHOLD})"

    return True, f"очередь низкая ({depth} видео < порога {config.SP_PIPELINE_QUEUE_THRESHOLD})"


def _start() -> bool:
    """Запускает run_pipeline.py в директории ShortsProject. Возвращает True при успехе."""
    global _process, _started_at

    run_pipeline = config.SHORTS_PROJECT_DIR / "run_pipeline.py"
    if not run_pipeline.exists():
        logger.error("[sp_runner] run_pipeline.py не найден: %s", run_pipeline)
        return False

    log_path = config.SP_LOG_FILE
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(str(log_path), "a", encoding="utf-8")
        _process = subprocess.Popen(
            [sys.executable, str(run_pipeline)],
            cwd=str(config.SHORTS_PROJECT_DIR),
            stdout=log_file,
            stderr=log_file,
            # Новый процесс не привязан к Orchestrator — переживёт его перезапуск
            close_fds=True,
        )
        _started_at = time.time()
        _save_pid(_process.pid, _started_at)

        logger.info("[sp_runner] ShortsProject pipeline запущен (PID %d)", _process.pid)
        send_message(
            f"🚀 <b>ShortsProject Pipeline запущен</b>\n"
            f"PID: {_process.pid}\n"
            f"Лог: {log_path.name}"
        )
        return True

    except Exception as e:
        logger.error("[sp_runner] Не удалось запустить pipeline: %s", e)
        send_message(f"🔴 <b>SP Pipeline — ошибка запуска</b>\n{e}")
        return False


def _check_hung() -> None:
    """Проверяет, не завис ли pipeline. Если да — отправляет алерт."""
    global _started_at

    if _started_at is None:
        return

    elapsed_h = (time.time() - _started_at) / 3600
    max_h = config.SP_PIPELINE_MAX_DURATION_HOURS

    if elapsed_h > max_h:
        logger.warning(
            "[sp_runner] Pipeline работает %.1fч (макс %dч) — возможно завис!",
            elapsed_h, max_h,
        )
        send_message(
            f"⚠️ <b>SP Pipeline — возможное зависание</b>\n"
            f"Работает {elapsed_h:.1f}ч (порог {max_h}ч)\n"
            f"Проверьте процесс вручную."
        )


def _on_finished(exit_code: int) -> None:
    """Вызывается когда pipeline завершил работу."""
    global _last_finished_at, _process, _started_at

    _last_finished_at = time.time()
    duration_h = (_last_finished_at - (_started_at or _last_finished_at)) / 3600
    _started_at = None
    _process = None
    _clear_pid()

    if exit_code == 0:
        logger.info("[sp_runner] Pipeline завершён успешно (%.1fч)", duration_h)
        send_message(
            f"✅ <b>ShortsProject Pipeline завершён</b>\n"
            f"Длительность: {duration_h:.1f}ч"
        )
    else:
        logger.error("[sp_runner] Pipeline завершился с ошибкой (код %d)", exit_code)
        send_message(
            f"🔴 <b>SP Pipeline — аварийное завершение</b>\n"
            f"Код выхода: {exit_code}\n"
            f"Длительность: {duration_h:.1f}ч"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Публичный интерфейс
# ─────────────────────────────────────────────────────────────────────────────

def manage_sp_pipeline(metrics_data: dict = None) -> None:
    """
    Главная функция — вызывается из main_orchestrator каждый цикл.
    Проверяет состояние текущего процесса и решает, нужен ли новый запуск.

    Args:
        metrics_data: данные из tracking (не используется пока, зарезервировано)
    """
    if not config.SP_PIPELINE_ENABLED:
        return

    # 1. Проверяем живой ли текущий процесс
    if _process is not None and not is_running():
        rc = _process.poll()
        _on_finished(rc if rc is not None else -1)

    # 2. Детект зависания
    if is_running():
        _check_hung()
        logger.debug("[sp_runner] Pipeline активен — запуск нового не нужен")
        return

    # 3. Решаем: запускать или нет
    should_run, reason = _should_trigger()
    logger.info("[sp_runner] Проверка триггера: %s (%s)", "ЗАПУСК" if should_run else "пропуск", reason)

    if should_run:
        _start()
