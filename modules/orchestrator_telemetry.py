"""
orchestrator_telemetry.py — trace-id и текущий шаг цикла Orchestrator.

Пишет атомарно JSON в data/orchestrator_telemetry.json — удобно читать из ContentHub.
Не заменяет логи; дополняет их для UI/отладки.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import config

logger = logging.getLogger(__name__)

_TELEMETRY_PATH: Path = config.BASE_DIR / "data" / "orchestrator_telemetry.json"
_TRACE_JSONL_PATH: Path = config.BASE_DIR / "data" / "orchestrator_trace.jsonl"
_POLICY_CMD_TRACE_PATH: Path = config.BASE_DIR / "data" / "policy_command_trace.jsonl"

# Ротация JSONL: при превышении размера оставляем последние N строк (ORC_TRACE_*)
def _trace_max_bytes() -> int:
    return int(os.getenv("ORC_TRACE_MAX_BYTES", "1500000"))


def _trace_keep_tail_lines() -> int:
    return int(os.getenv("ORC_TRACE_KEEP_TAIL_LINES", "3000"))


def _rotate_jsonl_if_needed(path: Path) -> None:
    """Если файл разросся — обрезаем до хвоста (идея п.2: без ручной очистки)."""
    try:
        if not path.exists():
            return
        if path.stat().st_size <= _trace_max_bytes():
            return
        keep = max(100, _trace_keep_tail_lines())
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        tail = lines[-keep:]
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(tail) + ("\n" if tail else ""), encoding="utf-8")
        os.replace(tmp, path)
        logger.info(
            "[Telemetry] Ротация %s: было %d строк, оставлено %d",
            path.name,
            len(lines),
            len(tail),
        )
    except Exception as exc:
        logger.warning("[Telemetry] Ротация %s: %s", path, exc)


def _append_jsonl_record(path: Path, record: Dict[str, Any]) -> None:
    _rotate_jsonl_if_needed(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _atomic_write(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.write(fd, text.encode("utf-8"))
        os.close(fd)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def begin_cycle(cycle_num: int) -> str:
    """Старт цикла: новый trace_id, статус running."""
    trace_id = uuid.uuid4().hex[:12]
    payload = {
        "trace_id": trace_id,
        "cycle_num": cycle_num,
        "current_node": "starting",
        "step_label": "Старт цикла",
        "status": "running",
        "cycle_outcome": None,
        "cycle_summary": {},
        "node_outcomes": {},
        "started_at": _utc_now(),
        "updated_at": _utc_now(),
        "finished_at": None,
    }
    try:
        _atomic_write(_TELEMETRY_PATH, payload)
    except Exception as exc:
        logger.warning("[Telemetry] Не удалось записать begin_cycle: %s", exc)
    logger.info("[Telemetry] trace_id=%s cycle=%s", trace_id, cycle_num)
    return trace_id


def mark_step(
    trace_id: str,
    node_id: str,
    step_label: str,
    *,
    node_outcome: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    """Текущий узел графа и подпись человеческим языком.

    node_outcome — семантический код узла (п.5: итог в telemetry, детали — в trace).
    detail — произвольный dict для JSONL (полная траектория), не дублировать в основной JSON.
    """
    try:
        cur = _read_safe()
        cur["trace_id"] = trace_id
        cur["current_node"] = node_id
        cur["step_label"] = step_label
        cur["updated_at"] = _utc_now()
        if cur.get("status") != "running":
            cur["status"] = "running"
        if node_outcome:
            nodes = cur.get("node_outcomes") or {}
            nodes[node_id] = node_outcome
            cur["node_outcomes"] = nodes
        _atomic_write(_TELEMETRY_PATH, cur)
        if detail is not None:
            _append_trace_jsonl(
                {
                    "trace_id": trace_id,
                    "node": node_id,
                    "step_label": step_label,
                    "node_outcome": node_outcome,
                    "ts": _utc_now(),
                    "detail": detail,
                }
            )
    except Exception as exc:
        logger.debug("[Telemetry] mark_step: %s", exc)


def record_cycle_summary(trace_id: str, summary: Dict[str, Any]) -> None:
    """Компактный итог цикла для UI (без длинных трасс)."""
    try:
        cur = _read_safe()
        cur["trace_id"] = trace_id
        cur["cycle_summary"] = summary
        if "cycle_outcome" in summary:
            cur["cycle_outcome"] = summary["cycle_outcome"]
        cur["updated_at"] = _utc_now()
        _atomic_write(_TELEMETRY_PATH, cur)
    except Exception as exc:
        logger.warning("[Telemetry] record_cycle_summary: %s", exc)


def _append_trace_jsonl(record: Dict[str, Any]) -> None:
    """Полная траектория шагов — отдельный JSONL (п.5)."""
    try:
        _append_jsonl_record(_TRACE_JSONL_PATH, record)
    except Exception as exc:
        logger.debug("[Telemetry] trace jsonl: %s", exc)


def append_policy_command_event(
    trace_id: str,
    command_id: int,
    stage: str,
    outcome: str,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    """Отдельная траектория разбора команд оператора (те же коды, что cycle_semantics)."""
    try:
        _append_jsonl_record(
            _POLICY_CMD_TRACE_PATH,
            {
                "kind": "policy_command",
                "trace_id": trace_id or None,
                "command_id": command_id,
                "stage": stage,
                "outcome": outcome,
                "ts": _utc_now(),
                "detail": detail or {},
            },
        )
    except Exception as exc:
        logger.debug("[Telemetry] policy trace: %s", exc)


def end_cycle(
    trace_id: str,
    status: str = "completed",
    *,
    cycle_outcome: Optional[str] = None,
    cycle_summary: Optional[Dict[str, Any]] = None,
) -> None:
    """Завершение цикла: completed | error | cancelled."""
    try:
        cur = _read_safe()
        cur["trace_id"] = trace_id
        cur["status"] = status
        cur["finished_at"] = _utc_now()
        cur["updated_at"] = _utc_now()
        if cycle_outcome is not None:
            cur["cycle_outcome"] = cycle_outcome
        if cycle_summary:
            cur["cycle_summary"] = {**(cur.get("cycle_summary") or {}), **cycle_summary}
        if status == "completed":
            cur["current_node"] = "done"
            cur["step_label"] = "Цикл завершён"
        _atomic_write(_TELEMETRY_PATH, cur)
        _append_trace_jsonl(
            {
                "trace_id": trace_id,
                "node": "end_cycle",
                "step_label": "Конец цикла",
                "ts": _utc_now(),
                "detail": {"status": status, "cycle_outcome": cur.get("cycle_outcome")},
            }
        )
    except Exception as exc:
        logger.warning("[Telemetry] end_cycle: %s", exc)


def _read_safe() -> Dict[str, Any]:
    if not _TELEMETRY_PATH.exists():
        return {}
    try:
        return json.loads(_TELEMETRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def read_telemetry() -> Dict[str, Any]:
    """Публичное чтение снимка (для ContentHub)."""
    return _read_safe()
