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
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from db.connection  import get_db
from db.experiences import (
    update_metric_impact,
    save_plan_quality_score,
    update_plan_quality_llm_judge,
)
import config as _config
from integrations.ollama_client import call_llm

logger = logging.getLogger(__name__)

_MIN_AGE_HOURS = 24  # ждём минимум 24ч до оценки


# ─────────────────────────────────────────────────────────────────────────────
# Точка входа
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_pending_changes() -> int:
    """
    Оценивает все applied_changes старше 24ч без metric_impact_json.
    После оценки — агрегирует дельты по plan_id и записывает в plan_quality_scores.
    Возвращает количество оценённых изменений.
    """
    pending = _get_unevaluated_changes()
    if not pending:
        return 0

    evaluated = 0
    plan_deltas: Dict[int, list] = {}   # plan_id → list of deltas

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
            plan_id = change.get("plan_id")
            if plan_id:
                plan_deltas.setdefault(plan_id, []).append(delta)
        else:
            logger.debug("[Evaluator] Нет снапшотов для оценки #%d", change["id"])

    # Агрегируем дельты по плану → записываем plan_quality_scores + LLM-as-judge
    for plan_id, deltas in plan_deltas.items():
        _save_plan_quality(plan_id, deltas)

    if evaluated:
        logger.info("[Evaluator] Оценено: %d изменений", evaluated)
    return evaluated


def _save_plan_quality(plan_id: int, deltas: list) -> None:
    """Вычисляет overall_score и записывает в plan_quality_scores."""
    views_vals  = [d["views_delta_pct"] for d in deltas if "views_delta_pct" in d]
    ctr_vals    = [d["ctr_delta_pct"]   for d in deltas if "ctr_delta_pct"   in d]
    cr_vals     = [d["cr_delta_pct"]    for d in deltas if "cr_delta_pct"    in d]
    ban_vals    = [d["ban_delta"]        for d in deltas if "ban_delta"        in d]

    avg = lambda lst: sum(lst) / len(lst) if lst else None

    views_delta = avg(views_vals)
    ctr_delta   = avg(ctr_vals)
    cr_delta    = avg(cr_vals)
    ban_delta   = int(sum(ban_vals)) if ban_vals else None

    # Взвешенная оценка: просмотры и CTR — 30% каждый, CR — 30%, баны — штраф
    score = 0.0
    if views_delta is not None:
        score += views_delta * 0.3
    if ctr_delta is not None:
        score += ctr_delta * 0.3
    if cr_delta is not None:
        score += cr_delta * 0.3
    if ban_delta is not None:
        score -= ban_delta * 10   # каждый новый бан = −10 баллов

    # Собираем затронутые зоны из applied_changes
    zones_affected: list = []
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT zone FROM applied_changes WHERE plan_id = ?",
                (plan_id,),
            ).fetchall()
            zones_affected = [r["zone"] for r in rows if r["zone"]]
    except Exception:
        pass

    row_id = save_plan_quality_score(
        plan_id        = plan_id,
        views_delta_pct= views_delta,
        ctr_delta_pct  = ctr_delta,
        cr_delta_pct   = cr_delta,
        ban_delta      = ban_delta,
        overall_score  = round(score, 2),
        model_used     = getattr(_config, "OLLAMA_STRATEGY_MODEL", ""),
        zones_affected = zones_affected,
    )
    logger.info("[Evaluator] plan_quality_scores для плана #%d: score=%.2f", plan_id, score)
    _score_plan_quality_llm(plan_id, deltas, row_id)


# ─────────────────────────────────────────────────────────────────────────────
# Выборка необработанных изменений
# ─────────────────────────────────────────────────────────────────────────────

def _parse_llm_json_obj(raw: str) -> Optional[Dict[str, Any]]:
    """Извлекает первый JSON-объект из ответа LLM. Делегирует в utils/llm_json.py (DRY)."""
    from utils.llm_json import extract_json_object
    return extract_json_object(raw)


def _score_plan_quality_llm(plan_id: int, deltas: list, pqs_row_id: int) -> None:
    """LLM-as-judge: оценка плана по агрегированным дельтам метрик."""
    merged: Dict[str, Any] = {}
    for d in deltas:
        if isinstance(d, dict):
            merged.update(d)

    with get_db() as conn:
        plan = conn.execute(
            "SELECT summary, risk_level FROM evolution_plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
    if not plan:
        return

    prompt = f"""Ты — эксперт по оценке решений в системе автоматизации.

Был сгенерирован план:
  Описание: {plan['summary'][:800]}
  Уровень риска: {plan['risk_level']}

Результат через 24 часа (дельта метрик):
  {json.dumps(merged, ensure_ascii=False, indent=2)}

Оцени качество этого плана от 1 до 10:
  1-3: план навредил или не дал эффекта
  4-6: нейтральный или минимальный эффект
  7-8: хороший результат
  9-10: отличный результат

Верни ТОЛЬКО JSON: {{"score": N, "reasoning": "краткое обоснование"}}"""

    raw = call_llm(model=_config.OLLAMA_STRATEGY_MODEL, prompt=prompt)
    parsed = _parse_llm_json_obj(raw or "")
    if not parsed or "score" not in parsed:
        logger.debug("[Evaluator] LLM judge: нет валидного JSON для плана #%d", plan_id)
        return

    try:
        score = max(1, min(10, int(parsed["score"])))
    except (TypeError, ValueError):
        return
    reasoning = str(parsed.get("reasoning", ""))[:500]

    try:
        update_plan_quality_llm_judge(pqs_row_id, score, reasoning)
        logger.info("[Evaluator] LLM judge план #%d: score=%d", plan_id, score)
    except Exception as exc:
        logger.warning("[Evaluator] LLM judge save: %s", exc)


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
