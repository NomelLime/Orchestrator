"""
modules/evaluator.py — Ретроспективная оценка применённых изменений.

Через 24ч после каждого изменения сравнивает снапшоты метрик
до и после, записывает дельту в applied_changes.metric_impact_json.

Это позволяет LLM не повторять стратегии, которые ухудшили показатели,
и опираться на те, что реально помогли.

Экспортирует:
    evaluate_pending_changes() → int (число оценённых изменений)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from db.connection  import get_db
from db.experiences import update_metric_impact

logger = logging.getLogger(__name__)

_MIN_AGE_HOURS = 24  # ждём минимум 24ч до оценки


# ─────────────────────────────────────────────────────────────────────────────
# Точка входа
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_pending_changes() -> int:
    """
    Оценивает все applied_changes старше 24ч без metric_impact_json.
    Возвращает количество оценённых изменений.
    """
    pending = _get_unevaluated_changes()
    if not pending:
        return 0

    evaluated = 0
    for change in pending:
        delta = _compute_delta(change)
        if delta:
            update_metric_impact(change["id"], delta)
            logger.info(
                "[Evaluator] Изменение #%d [%s]: %s",
                change["id"], change["zone"],
                json.dumps(delta, ensure_ascii=False)[:80],
            )
            evaluated += 1
        else:
            logger.debug("[Evaluator] Нет снапшотов для оценки #%d", change["id"])

    if evaluated:
        logger.info("[Evaluator] Оценено: %d изменений", evaluated)
    return evaluated


# ─────────────────────────────────────────────────────────────────────────────
# Выборка необработанных изменений
# ─────────────────────────────────────────────────────────────────────────────

def _get_unevaluated_changes() -> List[Dict]:
    cutoff = _shift_hours(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), -_MIN_AGE_HOURS)
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, applied_at, repo, zone, description, change_type
            FROM applied_changes
            WHERE metric_impact_json IS NULL
              AND rolled_back = 0
              AND applied_at <= ?
            ORDER BY applied_at ASC
            LIMIT 20
        """, (cutoff,)).fetchall()
    return [dict(row) for row in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Расчёт дельты
# ─────────────────────────────────────────────────────────────────────────────

def _compute_delta(change: Dict) -> Optional[Dict]:
    """
    Ищет снапшоты до applied_at и через 24ч после.
    Возвращает словарь с дельтами или None если данных нет.
    """
    applied_at = change["applied_at"]
    after_ts   = _shift_hours(applied_at, _MIN_AGE_HOURS)
    source     = "ShortsProject" if change["repo"] == "ShortsProject" else "PreLend"

    before = _nearest_snapshot(source, applied_at, "before")
    after  = _nearest_snapshot(source, after_ts,   "after")

    if not before or not after:
        return None

    return _sp_delta(before, after) if source == "ShortsProject" else _pl_delta(before, after)


def _sp_delta(b: Dict, a: Dict) -> Dict:
    d: Dict[str, Any] = {}
    bv, av = b.get("sp_total_views") or 0, a.get("sp_total_views") or 0
    if bv > 0:
        d["views_delta_pct"] = round((av - bv) / bv * 100, 2)
    bc, ac = b.get("sp_avg_ctr") or 0.0, a.get("sp_avg_ctr") or 0.0
    if bc > 0:
        d["ctr_delta_pct"] = round((ac - bc) / bc * 100, 2)
    d["ban_delta"] = (a.get("sp_ban_count") or 0) - (b.get("sp_ban_count") or 0)
    return d


def _pl_delta(b: Dict, a: Dict) -> Dict:
    d: Dict[str, Any] = {}
    bcr, acr = b.get("pl_cr") or 0.0, a.get("pl_cr") or 0.0
    if bcr > 0:
        d["cr_delta_pct"] = round((acr - bcr) / bcr * 100, 2)
    d["bot_pct_delta"] = round((a.get("pl_bot_pct") or 0.0) - (b.get("pl_bot_pct") or 0.0), 2)
    bclk, aclk = b.get("pl_total_clicks") or 0, a.get("pl_total_clicks") or 0
    if bclk > 0:
        d["clicks_delta_pct"] = round((aclk - bclk) / bclk * 100, 2)
    return d


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────────────────────────────────────

def _nearest_snapshot(source: str, ts: str, direction: str) -> Optional[Dict]:
    op  = "<=" if direction == "before" else ">="
    ord_clause = "DESC" if direction == "before" else "ASC"
    with get_db() as conn:
        row = conn.execute(
            f"SELECT * FROM metrics_snapshots "
            f"WHERE source = ? AND snapshot_at {op} ? "
            f"ORDER BY snapshot_at {ord_clause} LIMIT 1",
            (source, ts),
        ).fetchone()
    return dict(row) if row else None


def _shift_hours(ts_str: str, hours: int) -> str:
    """Сдвигает ISO-строку на N часов."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            dt = datetime.strptime(ts_str, fmt)
            return (dt + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return ts_str
