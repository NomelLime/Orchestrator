"""
db/experiences.py — Запись и чтение планов эволюции и применённых изменений.

Экспортирует:
    save_evolution_plan(...)         → id нового плана
    mark_plan_applied(plan_id)
    mark_plan_failed(plan_id)
    save_applied_change(...)         → id записи
    update_metric_impact(id, delta)  → записывает результат 24h оценки
    get_recent_experience(n)         → список последних N результатов (legacy)
    get_rich_experience_context(n)   → список с metric_impact для LLM-промпта
    get_failed_patterns()            → описания неудач
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from db.connection import get_db

logger = logging.getLogger(__name__)


def save_evolution_plan(
    summary: str,
    raw_plan: Dict,
    zones_affected: List[str],
    files_affected: List[str],
    risk_level: str = "low",
) -> int:
    """Сохраняет новый план эволюции. Возвращает его id."""
    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO evolution_plans
                (summary, raw_plan_json, zones_affected, files_affected, risk_level, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
        """, (
            summary,
            json.dumps(raw_plan, ensure_ascii=False),
            json.dumps(zones_affected),
            json.dumps(files_affected),
            risk_level,
        ))
        plan_id = cursor.lastrowid
    logger.info("[Experience] Новый план #%d: %s (риск: %s)", plan_id, summary[:60], risk_level)
    return plan_id


def mark_plan_applied(plan_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE evolution_plans SET status = 'applied' WHERE id = ?",
            (plan_id,)
        )


def mark_plan_failed(plan_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE evolution_plans SET status = 'failed' WHERE id = ?",
            (plan_id,)
        )


def save_applied_change(
    plan_id: int,
    change_type: str,       # 'config_change' | 'code_patch'
    repo: str,              # 'ShortsProject' | 'PreLend'
    zone: str,
    description: str,
    file_path: Optional[str] = None,
    old_value: Any = None,
    new_value: Any = None,
    test_status: Optional[str] = None,   # 'passed' | 'failed' | 'skipped'
    test_output: Optional[str] = None,
    rolled_back: bool = False,
    rollback_reason: Optional[str] = None,
) -> int:
    """Сохраняет результат применения одного изменения из плана. Возвращает id."""
    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO applied_changes (
                evolution_plan_id, change_type, repo, zone, description,
                file_path, old_value_json, new_value_json,
                test_status, test_output,
                rolled_back, rollback_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            plan_id, change_type, repo, zone, description,
            file_path,
            json.dumps(old_value, ensure_ascii=False) if old_value is not None else None,
            json.dumps(new_value, ensure_ascii=False) if new_value is not None else None,
            test_status,
            (test_output or "")[:2000],  # не храним бесконечные логи
            int(rolled_back),
            rollback_reason,
        ))
        change_id = cursor.lastrowid

    status_str = f"{'ОТКАТ' if rolled_back else test_status or 'ok'}"
    logger.info("[Experience] Изменение #%d (%s) plan=#%d %s: %s",
                change_id, change_type, plan_id, status_str, description[:60])
    return change_id


def get_recent_experience(last_n: int = 20) -> List[Dict]:
    """
    Возвращает последние N применённых изменений в формате для LLM-промпта.
    Включает: тип, зону, описание, статус тестов, был ли откат.

    Orchestrator передаёт это в промпт LLM чтобы та не повторяла неудачные паттерны.
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                ac.change_type,
                ac.repo,
                ac.zone,
                ac.description,
                ac.test_status,
                ac.rolled_back,
                ac.rollback_reason,
                ac.applied_at,
                ep.summary AS plan_summary
            FROM applied_changes ac
            LEFT JOIN evolution_plans ep ON ac.evolution_plan_id = ep.id
            ORDER BY ac.applied_at DESC
            LIMIT ?
        """, (last_n,)).fetchall()

    return [dict(row) for row in rows]


def update_metric_impact(change_id: int, delta: Dict) -> None:
    """Записывает результат ретроспективной оценки (evaluator.py → сюда)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE applied_changes SET metric_impact_json = ? WHERE id = ?",
            (json.dumps(delta, ensure_ascii=False), change_id),
        )
    logger.info("[Experience] Metric impact записан для изменения #%d", change_id)


def get_rich_experience_context(last_n: int = 10) -> List[Dict]:
    """
    Возвращает последние N изменений с результатами оценки (metric_impact_json).
    Используется в evolution.py для показа LLM реальных результатов.
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                ac.id,
                ac.change_type,
                ac.repo,
                ac.zone,
                ac.description,
                ac.test_status,
                ac.rolled_back,
                ac.rollback_reason,
                ac.applied_at,
                ac.metric_impact_json,
                ep.summary AS plan_summary
            FROM applied_changes ac
            LEFT JOIN evolution_plans ep ON ac.evolution_plan_id = ep.id
            ORDER BY ac.applied_at DESC
            LIMIT ?
        """, (last_n,)).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        d["metric_impact"] = None
        if d.get("metric_impact_json"):
            try:
                d["metric_impact"] = json.loads(d["metric_impact_json"])
            except Exception:
                pass
        result.append(d)
    return result


def get_failed_patterns() -> List[str]:
    """
    Возвращает список описаний неудачных изменений (для включения в LLM-промпт
    как 'что не делать').
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT description, rollback_reason, zone
            FROM applied_changes
            WHERE rolled_back = 1 OR test_status = 'failed'
            ORDER BY applied_at DESC
            LIMIT 10
        """).fetchall()
    return [
        f"[{row['zone']}] {row['description']} → {row['rollback_reason'] or 'тесты не прошли'}"
        for row in rows
    ]
