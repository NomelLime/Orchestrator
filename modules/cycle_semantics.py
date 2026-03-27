"""
cycle_semantics.py — семантические статусы цикла Orchestrator (п.1–2, п.5).

«Ускальзывание» ловится явным outcome, а не расширением ReAct.
"""

from __future__ import annotations

from typing import Any, Dict, List

# ── Коды исхода (строки — сериализуемы в JSON / ContentHub) ───────────────────

OK = "ok"
TRANSPORT_FAILURE = "transport_failure"
DATA_CONTRACT_VIOLATION = "data_contract_violation"
EXECUTION_MISMATCH = "execution_mismatch"
POLICY_GAP = "policy_gap"
NEEDS_HUMAN = "needs_human"
STUCK_LOOP = "stuck_loop"
MODE_VIOLATION = "mode_violation"
APPLY_NOT_VERIFIED = "apply_not_verified"
INCOMPLETE_EVIDENCE = "incomplete_evidence"
BLOCKED_BY_POLICY = "blocked_by_policy"
NEEDS_CLARIFICATION = "needs_clarification"
TOOL_MISSING = "tool_missing"
LLM_EMPTY = "llm_empty"
SKIPPED_HEURISTIC = "skipped_heuristic"
CANCELLED = "cancelled"
PAUSED = "paused"
ERROR = "error"

# Приоритет для агрегации: больше = хуже (для выбора итогового статуса)
_SEVERITY: Dict[str, int] = {
    OK: 0,
    SKIPPED_HEURISTIC: 1,
    PAUSED: 2,
    CANCELLED: 2,
    INCOMPLETE_EVIDENCE: 3,
    NEEDS_CLARIFICATION: 4,
    NEEDS_HUMAN: 5,
    POLICY_GAP: 6,
    LLM_EMPTY: 6,
    TOOL_MISSING: 7,
    BLOCKED_BY_POLICY: 7,
    TRANSPORT_FAILURE: 8,
    DATA_CONTRACT_VIOLATION: 8,
    MODE_VIOLATION: 8,
    APPLY_NOT_VERIFIED: 9,
    EXECUTION_MISMATCH: 10,
    STUCK_LOOP: 10,
    ERROR: 11,
}


def severity(code: str) -> int:
    return _SEVERITY.get(code, 5)


def merge_outcomes(codes: List[str]) -> str:
    """Возвращает самый «плохой» код из списка (непустой)."""
    valid = [c for c in codes if c]
    if not valid:
        return OK
    return max(valid, key=lambda c: severity(c))


def summarize_cycle(
    trace_id: str,
    cycle_num: int,
    outcome: str,
    extra: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Компактный итог для state и UI (без длинных трасс)."""
    s: Dict[str, Any] = {
        "trace_id": trace_id,
        "cycle_num": cycle_num,
        "cycle_outcome": outcome,
    }
    if extra:
        s.update(extra)
    return s
