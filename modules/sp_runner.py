"""
modules/sp_runner.py — Управление ShortsProject pipeline по этапам (--only + checkpoint).
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
from typing import Any, Dict, Optional

import config
from commander.notifier import send_message

logger = logging.getLogger(__name__)

_process: Optional[subprocess.Popen] = None
_started_at: Optional[float] = None
_last_finished_at: Optional[float] = None

_staged_active: bool = False
_staged_started_at: Optional[float] = None


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
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def is_running() -> bool:
    global _process, _started_at

    if _staged_active:
        return True

    if _process is not None:
        return _process.poll() is None

    saved = _load_pid()
    if saved and _pid_is_alive(saved["pid"]):
        logger.info(
            "[sp_runner] Восстановлен PID %d из файла (запущен %s)",
            saved["pid"],
            datetime.fromtimestamp(saved["started_at"]).strftime("%H:%M:%S"),
        )
        _started_at = saved["started_at"]
        return True

    _clear_pid()
    return False


def _count_queue_depth() -> int:
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
    global _last_finished_at

    if is_running():
        return False, "уже запущен"

    if _last_finished_at is not None:
        elapsed_h = (time.time() - _last_finished_at) / 3600
        if elapsed_h < config.SP_PIPELINE_INTERVAL_HOURS:
            remaining = config.SP_PIPELINE_INTERVAL_HOURS - elapsed_h
            return False, f"слишком рано (ещё {remaining:.1f}ч)"

    depth = _count_queue_depth()
    if depth >= config.SP_PIPELINE_QUEUE_THRESHOLD:
        return False, f"очередь полная ({depth} видео ≥ порога {config.SP_PIPELINE_QUEUE_THRESHOLD})"

    return True, f"очередь низкая ({depth} видео < порога {config.SP_PIPELINE_QUEUE_THRESHOLD})"


def _read_pipeline_state() -> dict:
    state_file = config.SHORTS_PROJECT_DIR / "data" / "pipeline_state.json"
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"stages": {}}


def _is_stage_done(state: dict, stage: str) -> bool:
    st = (state.get("stages") or {}).get(stage, {})
    return st.get("status") == "done"


def _reset_pipeline_state_subprocess() -> None:
    cmd = [
        sys.executable,
        "-c",
        "from pipeline.pipeline_state import reset_state; reset_state()",
    ]
    try:
        subprocess.run(
            cmd,
            cwd=str(config.SHORTS_PROJECT_DIR),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as e:
        logger.warning("[sp_runner] reset_state: %s", e)


def _run_single_stage(stage: str, timeout: int) -> int:
    run_pipeline = config.SHORTS_PROJECT_DIR / "run_pipeline.py"
    cmd = [sys.executable, str(run_pipeline), "--only", stage]
    try:
        log_path = config.SP_LOG_FILE
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(log_path), "a", encoding="utf-8") as logf:
            result = subprocess.run(
                cmd,
                timeout=timeout,
                cwd=str(config.SHORTS_PROJECT_DIR),
                stdout=logf,
                stderr=subprocess.STDOUT,
                text=True,
            )
        return int(result.returncode)
    except subprocess.TimeoutExpired:
        logger.error("[sp_runner] Stage %s timeout (%ds)", stage, timeout)
        return -1
    except Exception as e:
        logger.error("[sp_runner] Stage %s error: %s", stage, e)
        return -1


def _check_hung() -> None:
    global _started_at, _staged_started_at

    t0 = _started_at or _staged_started_at
    if t0 is None:
        return

    elapsed_h = (time.time() - t0) / 3600
    max_h = config.SP_PIPELINE_MAX_DURATION_HOURS

    if elapsed_h > max_h:
        logger.warning(
            "[sp_runner] Pipeline работает %.1fч (макс %dч) — возможно завис!",
            elapsed_h,
            max_h,
        )
        send_message(
            f"⚠️ <b>SP Pipeline — возможное зависание</b>\n"
            f"Работает {elapsed_h:.1f}ч (порог {max_h}ч)\n"
            f"Проверьте процесс вручную."
        )


def _on_finished_legacy(exit_code: int) -> None:
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


def manage_sp_pipeline(metrics_data: dict | None = None) -> Dict[str, Any]:
    """
    Запускает pipeline по этапам с retry и checkpoint.
    """
    global _last_finished_at, _process, _started_at, _staged_active, _staged_started_at

    if not config.SP_PIPELINE_ENABLED:
        return {"status": "disabled"}

    if _process is not None and not is_running():
        rc = _process.poll()
        _on_finished_legacy(rc if rc is not None else -1)

    if is_running():
        _check_hung()
        logger.debug("[sp_runner] Pipeline активен — запуск нового не нужен")
        return {"status": "running"}

    should_run, reason = _should_trigger()
    logger.info("[sp_runner] Проверка триггера: %s (%s)", "ЗАПУСК" if should_run else "пропуск", reason)

    if not should_run:
        return {"status": "skipped", "reason": reason}

    run_pipeline = config.SHORTS_PROJECT_DIR / "run_pipeline.py"
    if not run_pipeline.exists():
        logger.error("[sp_runner] run_pipeline.py не найден: %s", run_pipeline)
        return {"status": "error", "message": "no run_pipeline.py"}

    send_message(
        f"🚀 <b>ShortsProject Pipeline</b> (по этапам)\n"
        f"Лог: {config.SP_LOG_FILE.name}"
    )

    _staged_active = True
    _staged_started_at = time.time()
    _clear_pid()

    try:
        state = _read_pipeline_state()
        need_reset = not state.get("stages") or state.get("finished_at") is not None
        if need_reset:
            _reset_pipeline_state_subprocess()

        for stage in config.SP_PIPELINE_STAGES:
            state = _read_pipeline_state()
            if _is_stage_done(state, stage):
                logger.info("[sp_runner] Этап %s уже завершён — пропуск", stage)
                continue

            max_retries = config.SP_STAGE_MAX_RETRIES.get(stage, 1)
            timeout = config.SP_STAGE_TIMEOUTS.get(stage, 3600)
            ok = False

            for attempt in range(1, max_retries + 1):
                logger.info("[sp_runner] Этап %s — попытка %d/%d", stage, attempt, max_retries)
                exit_code = _run_single_stage(stage, timeout)
                if exit_code == 0:
                    ok = True
                    break
                logger.warning(
                    "[sp_runner] Этап %s попытка %d неудачна (код %d)",
                    stage,
                    attempt,
                    exit_code,
                )
                if attempt < max_retries:
                    time.sleep(config.SP_STAGE_BACKOFF_SEC)

            if not ok:
                if stage in config.SP_SKIPPABLE_STAGES:
                    logger.warning("[sp_runner] Пропуск %s после %d неудач", stage, max_retries)
                    send_message(
                        f"⚠️ Pipeline: этап <code>{stage}</code> пропущен ({max_retries} попыток)"
                    )
                    continue
                send_message(
                    f"🔴 Pipeline: этап <code>{stage}</code> провалил все {max_retries} попыток"
                )
                _last_finished_at = time.time()
                return {"status": "failed", "failed_stage": stage}

        _reset_pipeline_state_subprocess()
        _last_finished_at = time.time()
        send_message("✅ <b>ShortsProject Pipeline</b> — все этапы завершены")
        return {"status": "completed"}
    finally:
        _staged_active = False
        _staged_started_at = None
