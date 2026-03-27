"""
plan_heuristics.py — эвристики до вызова LLM для плана эволюции (п.6).

Если данных недостаточно для осмысленного промпта, лучше не тратить LLM и явно
задать cycle_outcome = incomplete_evidence.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

import config
from modules import cycle_semantics as cs

logger = logging.getLogger(__name__)


def should_skip_llm_plan(metrics_data: Dict[str, Any]) -> Tuple[bool, str, str]:
    """
    Returns:
        (skip, outcome_code, reason_for_human)
    """
    if config.PLAN_HEURISTICS_DISABLED:
        return False, cs.OK, ""

    sp = metrics_data.get("shorts_project") or {}
    pl = metrics_data.get("prelend") or {}

    sp_has_file = config.SP_ANALYTICS_FILE.exists()
    pl_unreachable = bool(pl.get("_unreachable"))

    if not sp_has_file and pl_unreachable:
        reason = (
            "Нет ShortsProject analytics.json и PreLend Internal API недоступен — "
            "недостаточно сигналов для плана."
        )
        logger.info("[PlanHeuristic] Пропуск LLM: %s", reason)
        return True, cs.INCOMPLETE_EVIDENCE, reason

    return False, cs.OK, ""
