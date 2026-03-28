"""
modules/orchestrator_graph.py — LangGraph оркестрация цикла Orchestrator.

Важно:
  - Граф НЕ заменяет существующие интеграции между проектами.
  - Узлы вызывают уже существующие модули (tracking/evolution/config_enforcer и т.д.).
  - inter-agent messaging остаётся прежним (AgentMemory/API/БД/Telegram).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, TypedDict

import config
from commander import notifier
from db.commands import cleanup_expired_policies, get_policy
from db.experiences import mark_plan_applied, mark_plan_failed
from langgraph.graph import END, StateGraph
from modules import (
    code_evolver,
    config_enforcer,
    cycle_semantics as cs,
    evaluator,
    evolution,
    orchestrator_telemetry,
    plan_heuristics,
    policies,
    sp_runner,
    supply_tracker,
    tracking,
    zones as zones_module,
)

logger = logging.getLogger("Orchestrator")


def _notify_outcome_if_needed(final_outcome: str, trace_id: str, summary: Dict[str, Any]) -> None:
    """
    Telegram при итоге цикла != ok.
    ORC_ALERT_ON_BAD_OUTCOME=false — отключить.
    ORC_ALERT_OUTCOME_ALLOWLIST=paused,cancelled — не беспокоить по этим кодам (по умолчанию).
    Пустой allowlist — алерт на любой не-ok.
    """
    if os.getenv("ORC_ALERT_ON_BAD_OUTCOME", "true").lower() != "true":
        return
    if final_outcome == cs.OK:
        return
    allow_raw = os.getenv("ORC_ALERT_OUTCOME_ALLOWLIST", "paused,cancelled")
    allow = {x.strip() for x in allow_raw.split(",") if x.strip()}
    if final_outcome in allow:
        return
    try:
        payload = json.dumps(summary, ensure_ascii=False)[:3500]
    except Exception:
        payload = str(summary)[:500]
    notifier.send_message(
        f"⚠️ <b>Orchestrator — итог цикла не ok</b>\n"
        f"<code>{final_outcome}</code>\n"
        f"trace: <code>{trace_id}</code>\n"
        f"<pre>{payload}</pre>"
    )
    notifier.log_notification(
        f"Итог цикла {final_outcome} (trace {trace_id})",
        level="warning",
        category="metric",
    )


def _add_outcomes(state: OrchestratorState, *codes: str) -> Dict[str, Any]:
    """Накапливает только значимые (не ok) коды для итогового merge_outcomes."""
    cur = list(state.get("outcomes") or [])
    for c in codes:
        if c and c != cs.OK:
            cur.append(c)
    return {"outcomes": cur}


class OrchestratorState(TypedDict, total=False):
    trace_id: str
    cycle_num: int
    outcomes: List[str]
    metrics_data: Dict[str, Any]
    plan: Dict[str, Any]
    plan_id: int
    pause_evolution: bool
    crash_reverted: bool
    no_plan: bool
    plan_cancelled: bool
    dry_run: bool
    cfg_ok: int
    cfg_fail: int
    code_ok: int
    code_fail: int
    queued_patches: int
    heuristic_reason: str


def _cleanup_old_data() -> None:
    """Суточная очистка таблиц БД порциями (без долгой WAL-блокировки)."""
    from db.connection import get_db as _get_db

    _tables = [
        ("metrics_snapshots", "90 days", ""),
        ("notifications", "30 days", ""),
        ("evolution_plans", "180 days", "AND status != 'applied'"),
    ]

    total_deleted = 0
    try:
        with _get_db() as conn:
            for table, age, extra in _tables:
                deleted_table = 0
                while True:
                    cur = conn.execute(
                        f"DELETE FROM {table} WHERE rowid IN "
                        f"(SELECT rowid FROM {table} "
                        f" WHERE created_at < datetime('now', '-{age}') {extra} "
                        f" LIMIT 500)"
                    )
                    batch = cur.rowcount
                    deleted_table += batch
                    if batch < 500:
                        break
                if deleted_table:
                    logger.info("[Cleanup] %s: удалено %d строк", table, deleted_table)
                total_deleted += deleted_table
            conn.commit()
        if total_deleted:
            logger.info("[Cleanup] Всего удалено: %d строк", total_deleted)
    except Exception as exc:
        logger.warning("[Cleanup] Ошибка очистки: %s", exc)


def _node_preflight(state: OrchestratorState) -> OrchestratorState:
    tid = state.get("trace_id", "")
    cycle_num = int(state.get("cycle_num", 0))
    evaluated = evaluator.evaluate_pending_changes()
    if evaluated:
        logger.info("[0/9] Ретроспективная оценка: %d изменений оценено", evaluated)

    cleanup_expired_policies()
    cycles_per_day = max(1, 24 // max(config.CYCLE_INTERVAL_HOURS, 1))
    if cycle_num % cycles_per_day == 0:
        _cleanup_old_data()

    logger.info("[1/9] Деградация зон...")
    zones_module.run_decay()

    logger.info("[2/9] Обработка команд оператора...")
    n_commands = policies.process_pending_commands(state.get("trace_id") or "")
    if n_commands:
        logger.info("  Обработано команд: %d", n_commands)

    paused = bool(get_policy("pause_evolution"))
    if paused:
        logger.info("[Orchestrator] Эволюция на паузе (команда оператора) — цикл пропущен")
    if tid:
        orchestrator_telemetry.mark_step(
            tid,
            "preflight",
            "Подготовка: оценка изменений, политики оператора, зоны доверия",
            node_outcome=cs.PAUSED if paused else cs.OK,
            detail={"commands_processed": n_commands},
        )
    upd: Dict[str, Any] = {"pause_evolution": paused}
    if paused:
        upd.update(_add_outcomes(state, cs.PAUSED))
    return upd


def _node_collect_metrics(state: OrchestratorState) -> OrchestratorState:
    tid = state.get("trace_id", "")
    logger.info("[3/9] Сбор метрик...")
    metrics_data = tracking.collect_all_and_save()
    sp = metrics_data["shorts_project"]
    pl = metrics_data["prelend"]
    logger.info(
        "  SP: views=%d, bans=%d | PreLend: clicks=%d, CR=%s",
        sp["total_views"],
        sp["ban_count"],
        pl["total_clicks"],
        f"{pl['cr']:.4f}" if pl.get("cr") else "н/д",
    )
    upd: Dict[str, Any] = {"metrics_data": metrics_data}
    pl_unreachable = bool(pl.get("_unreachable"))
    if pl_unreachable:
        upd.update(_add_outcomes(state, cs.TRANSPORT_FAILURE))
    if tid:
        orchestrator_telemetry.mark_step(
            tid,
            "collect_metrics",
            "Сбор метрик ShortsProject и PreLend",
            node_outcome=cs.TRANSPORT_FAILURE if pl_unreachable else cs.OK,
            detail={"prelend_unreachable": pl_unreachable},
        )
    return upd


def _node_crash_guard(state: OrchestratorState) -> OrchestratorState:
    tid = state.get("trace_id", "")
    reverted = bool(code_evolver.check_and_revert_on_crash())
    if reverted:
        logger.warning("[4/9] Краш-луп: автооткат выполнен — пропускаем генерацию плана")
    if tid:
        orchestrator_telemetry.mark_step(
            tid,
            "crash_guard",
            "Проверка краш-лупа агентов ShortsProject",
            node_outcome=cs.STUCK_LOOP if reverted else cs.OK,
            detail={"reverted": reverted},
        )
    upd: Dict[str, Any] = {"crash_reverted": reverted}
    if reverted:
        upd.update(_add_outcomes(state, cs.STUCK_LOOP))
    return upd


def _node_sp_pipeline(state: OrchestratorState) -> OrchestratorState:
    tid = state.get("trace_id", "")
    if tid:
        orchestrator_telemetry.mark_step(
            tid,
            "sp_pipeline",
            "Запуск/контроль пайплайна ShortsProject при низкой очереди",
            node_outcome=cs.OK,
        )
    logger.info("[5/9] SP Pipeline manager...")
    sp_runner.manage_sp_pipeline(state["metrics_data"])
    return {}


def _node_supply_check(state: OrchestratorState) -> OrchestratorState:
    tid = state.get("trace_id", "")
    if tid:
        orchestrator_telemetry.mark_step(
            tid,
            "supply_check",
            "Проверка прокси и баланса (по расписанию циклов)",
            node_outcome=cs.OK,
        )
    cycle_num = int(state.get("cycle_num", 0))
    if cycle_num % config.SUPPLY_CHECK_EVERY_N_CYCLES == 0:
        logger.info("[6/9] Проверка прокси/баланса...")
        supply_requests = supply_tracker.check_supply(state["metrics_data"]["shorts_project"])
        if supply_requests:
            logger.info("  Отправлено запросов оператору: %d", supply_requests)
    return {}


def _node_generate_plan(state: OrchestratorState) -> OrchestratorState:
    tid = state.get("trace_id", "")
    logger.info("[7/9] Генерация плана эволюции (LLM)...")
    metrics_data = state["metrics_data"]
    skip, skip_code, skip_reason = plan_heuristics.should_skip_llm_plan(metrics_data)
    if skip:
        logger.warning("[Orchestrator] План без LLM (эвристика): %s", skip_reason)
        notifier.log_notification(
            f"План пропущен (эвристика): {skip_reason[:200]}",
            level="warning",
            category="plan",
        )
        if tid:
            orchestrator_telemetry.mark_step(
                tid,
                "generate_plan",
                "Генерация плана — пропуск LLM (эвристика)",
                node_outcome=skip_code,
                detail={"heuristic": True, "reason": skip_reason},
            )
        return {
            "no_plan": True,
            **_add_outcomes(state, skip_code),
            "heuristic_reason": skip_reason,
        }

    plan = evolution.generate_plan(metrics_data)
    if not plan:
        logger.warning("[Orchestrator] LLM не вернула план — цикл завершён без изменений")
        notifier.log_notification("LLM не вернула план эволюции", level="warning", category="plan")
        if tid:
            orchestrator_telemetry.mark_step(
                tid,
                "generate_plan",
                "Генерация плана эволюции через LLM",
                node_outcome=cs.LLM_EMPTY,
                detail={"llm": "empty_or_unparseable"},
            )
        return {"no_plan": True, **_add_outcomes(state, cs.LLM_EMPTY)}

    if tid:
        orchestrator_telemetry.mark_step(
            tid,
            "generate_plan",
            "Генерация плана эволюции через LLM",
            node_outcome=cs.OK,
            detail={"plan_id": int(plan["_plan_id"])},
        )
    return {"plan": plan, "plan_id": int(plan["_plan_id"]), "no_plan": False}


def _node_announce_plan(state: OrchestratorState) -> OrchestratorState:
    tid = state.get("trace_id", "")
    plan = state["plan"]
    plan_id = state["plan_id"]
    logger.info(
        "  План #%d: %s (риск: %s)",
        plan_id,
        plan.get("summary", "")[:60],
        plan.get("risk_assessment", {}).get("estimated_risk", "?"),
    )
    notifier.send_message(
        f"📋 <b>Orchestrator — Новый план #{plan_id}</b>\n"
        f"Риск: {plan.get('risk_assessment', {}).get('estimated_risk', '?')}\n\n"
        f"{plan.get('summary', '')}",
    )
    notifier.log_notification(
        f"Сгенерирован план #{plan_id}: {plan.get('summary', '')[:80]}",
        category="plan",
    )

    dry_run = bool(config.DRY_RUN)
    if dry_run:
        logger.info("[Orchestrator] DRY_RUN — план не применяется")
    if tid:
        orchestrator_telemetry.mark_step(
            tid,
            "announce_plan",
            "Отправка плана в Telegram и лог уведомлений",
            node_outcome=cs.OK,
            detail={"plan_id": plan_id, "dry_run": dry_run},
        )
    return {"dry_run": dry_run}


def _node_wait_before_apply(state: OrchestratorState) -> OrchestratorState:
    tid = state.get("trace_id", "")
    if config.PLAN_APPLY_DELAY_SEC <= 0:
        if tid:
            orchestrator_telemetry.mark_step(
                tid,
                "wait_before_apply",
                "Пауза перед применением отключена (ORC_PLAN_APPLY_DELAY=0)",
                node_outcome=cs.OK,
                detail={"delay_sec": 0},
            )
        return {"plan_cancelled": False}

    # Импорт через main_orchestrator — чтобы использовать текущий сигнал /cancel_plan.
    from main_orchestrator import _cancel_plan as cancel_event  # pylint: disable=import-outside-toplevel

    plan_id = state["plan_id"]
    delay_min = config.PLAN_APPLY_DELAY_SEC // 60
    notifier.send_message(
        f"⏳ <b>Применение плана #{plan_id} через {delay_min} мин.</b>\n"
        f"Отправьте /freeze или /cancel_plan чтобы отменить."
    )
    logger.info("  Ожидание %d сек перед применением...", config.PLAN_APPLY_DELAY_SEC)
    cancel_event.clear()
    cancelled = bool(cancel_event.wait(timeout=config.PLAN_APPLY_DELAY_SEC))
    cancelled = cancelled or bool(get_policy("pause_evolution"))
    if cancelled:
        mark_plan_failed(plan_id)
        notifier.send_message(f"🛑 <b>План #{plan_id} отменён оператором</b>")
        logger.warning("[Orchestrator] План #%d отменён во время ожидания", plan_id)
    if tid:
        orchestrator_telemetry.mark_step(
            tid,
            "wait_before_apply",
            "Пауза перед применением плана",
            node_outcome=cs.CANCELLED if cancelled else cs.OK,
            detail={"cancelled": cancelled},
        )
    upd: Dict[str, Any] = {"plan_cancelled": cancelled}
    if cancelled:
        upd.update(_add_outcomes(state, cs.CANCELLED))
    return upd


def _node_apply_config(state: OrchestratorState) -> OrchestratorState:
    tid = state.get("trace_id", "")
    logger.info("[8/9] Применение config_changes...")
    cfg_ok, cfg_fail = config_enforcer.apply_config_changes(state["plan"], state["plan_id"])
    logger.info("  Конфиги: успешно=%d, ошибок=%d", cfg_ok, cfg_fail)
    if cfg_ok or cfg_fail:
        notifier.log_notification(
            f"Config changes план #{state['plan_id']}: +{cfg_ok} ошибок:{cfg_fail}",
            category="patch",
            level="info" if cfg_fail == 0 else "warning",
        )
    if tid:
        orchestrator_telemetry.mark_step(
            tid,
            "apply_config",
            "Применение изменений конфигов (ShortsProject / PreLend)",
            node_outcome=cs.EXECUTION_MISMATCH if cfg_fail > 0 else cs.OK,
            detail={"cfg_ok": cfg_ok, "cfg_fail": cfg_fail},
        )
    upd: Dict[str, Any] = {"cfg_ok": cfg_ok, "cfg_fail": cfg_fail}
    if cfg_fail > 0:
        upd.update(_add_outcomes(state, cs.EXECUTION_MISMATCH))
    return upd


def _node_apply_code(state: OrchestratorState) -> OrchestratorState:
    tid = state.get("trace_id", "")
    logger.info("[8/9] Code patches (Zone 4)...")
    code_ok, code_fail = code_evolver.apply_approved_patches()
    if code_ok or code_fail:
        logger.info("  Одобренные патчи применены: успешно=%d, откатов=%d", code_ok, code_fail)
        notifier.log_notification(
            f"Code patches применены: +{code_ok} откатов:{code_fail}",
            category="patch",
            level="info" if code_fail == 0 else "warning",
        )

    queued = code_evolver.queue_code_patches(state["plan"], state["plan_id"])
    if queued:
        logger.info("  Новых патчей поставлено в очередь: %d (ожидают /approve_N)", queued)
        notifier.log_notification(
            f"Plan #{state['plan_id']}: {queued} патч(ей) ожидают одобрения в Telegram",
            category="patch",
            level="info",
        )
    if tid:
        orchestrator_telemetry.mark_step(
            tid,
            "apply_code",
            "Патчи кода: применение одобренных и постановка новых в очередь",
            node_outcome=cs.EXECUTION_MISMATCH if code_fail > 0 else cs.OK,
            detail={"code_ok": code_ok, "code_fail": code_fail, "queued": queued},
        )
    upd: Dict[str, Any] = {"code_ok": code_ok, "code_fail": code_fail, "queued_patches": queued}
    if code_fail > 0:
        upd.update(_add_outcomes(state, cs.EXECUTION_MISMATCH))
    return upd


def _node_finalize_plan(state: OrchestratorState) -> OrchestratorState:
    tid = state.get("trace_id", "")
    total_ok = int(state.get("cfg_ok", 0)) + int(state.get("code_ok", 0))
    total_fail = int(state.get("cfg_fail", 0)) + int(state.get("code_fail", 0))
    if total_ok > 0:
        mark_plan_applied(state["plan_id"])
    elif total_fail > 0:
        mark_plan_failed(state["plan_id"])
    fin_out = cs.APPLY_NOT_VERIFIED if (total_fail > 0 and total_ok == 0) else cs.OK
    if tid:
        orchestrator_telemetry.mark_step(
            tid,
            "finalize_plan",
            "Фиксация статуса плана в базе данных",
            node_outcome=fin_out,
            detail={"total_ok": total_ok, "total_fail": total_fail},
        )
    upd: Dict[str, Any] = {}
    if total_fail > 0 and total_ok == 0:
        upd.update(_add_outcomes(state, cs.APPLY_NOT_VERIFIED))
    return upd


def _node_digest(state: OrchestratorState) -> OrchestratorState:
    tid = state.get("trace_id", "")
    if tid:
        orchestrator_telemetry.mark_step(
            tid,
            "digest",
            "Суточный дайджест в Telegram (если пора)",
            node_outcome=cs.OK,
        )
    logger.info("[9/9] Проверка суточного дайджеста...")
    md = state.get("metrics_data") or {}
    notifier.send_digest_with_analytics_card(
        md.get("shorts_project"),
        md.get("prelend"),
    )
    return {}


def _route_after_preflight(state: OrchestratorState) -> str:
    return "end" if state.get("pause_evolution") else "collect"


def _route_after_crash_guard(state: OrchestratorState) -> str:
    return "end" if state.get("crash_reverted") else "sp_pipeline"


def _route_after_generate_plan(state: OrchestratorState) -> str:
    return "end" if state.get("no_plan") else "announce"


def _route_after_announce(state: OrchestratorState) -> str:
    return "end" if state.get("dry_run") else "wait"


def _route_after_wait(state: OrchestratorState) -> str:
    return "end" if state.get("plan_cancelled") else "apply_config"


def build_cycle_graph():
    """Собирает и компилирует LangGraph для одного цикла Orchestrator."""
    graph = StateGraph(OrchestratorState)
    graph.add_node("preflight", _node_preflight)
    graph.add_node("collect_metrics", _node_collect_metrics)
    graph.add_node("crash_guard", _node_crash_guard)
    graph.add_node("sp_pipeline", _node_sp_pipeline)
    graph.add_node("supply_check", _node_supply_check)
    graph.add_node("generate_plan", _node_generate_plan)
    graph.add_node("announce_plan", _node_announce_plan)
    graph.add_node("wait_before_apply", _node_wait_before_apply)
    graph.add_node("apply_config", _node_apply_config)
    graph.add_node("apply_code", _node_apply_code)
    graph.add_node("finalize_plan", _node_finalize_plan)
    graph.add_node("digest", _node_digest)

    graph.set_entry_point("preflight")
    graph.add_conditional_edges(
        "preflight",
        _route_after_preflight,
        {"collect": "collect_metrics", "end": END},
    )
    graph.add_edge("collect_metrics", "crash_guard")
    graph.add_conditional_edges(
        "crash_guard",
        _route_after_crash_guard,
        {"sp_pipeline": "sp_pipeline", "end": END},
    )
    graph.add_edge("sp_pipeline", "supply_check")
    graph.add_edge("supply_check", "generate_plan")
    graph.add_conditional_edges(
        "generate_plan",
        _route_after_generate_plan,
        {"announce": "announce_plan", "end": END},
    )
    graph.add_conditional_edges(
        "announce_plan",
        _route_after_announce,
        {"wait": "wait_before_apply", "end": END},
    )
    graph.add_conditional_edges(
        "wait_before_apply",
        _route_after_wait,
        {"apply_config": "apply_config", "end": END},
    )
    graph.add_edge("apply_config", "apply_code")
    graph.add_edge("apply_code", "finalize_plan")
    graph.add_edge("finalize_plan", "digest")
    graph.add_edge("digest", END)
    return graph.compile()


_CYCLE_GRAPH = None


def run_cycle_graph(cycle_num: int) -> None:
    """Запускает один цикл через LangGraph."""
    global _CYCLE_GRAPH
    if _CYCLE_GRAPH is None:
        _CYCLE_GRAPH = build_cycle_graph()
    trace_id = orchestrator_telemetry.begin_cycle(cycle_num)
    try:
        final = _CYCLE_GRAPH.invoke(
            {"cycle_num": cycle_num, "trace_id": trace_id, "outcomes": []}
        )
        final_outcome = cs.merge_outcomes(final.get("outcomes") or [])
        summary = cs.summarize_cycle(
            trace_id,
            cycle_num,
            final_outcome,
            extra={
                "dry_run": bool(final.get("dry_run")),
                "no_plan": bool(final.get("no_plan")),
                "plan_cancelled": bool(final.get("plan_cancelled")),
                "pause_evolution": bool(final.get("pause_evolution")),
            },
        )
        orchestrator_telemetry.record_cycle_summary(trace_id, summary)
        orchestrator_telemetry.end_cycle(
            trace_id,
            status="completed",
            cycle_outcome=final_outcome,
            cycle_summary=summary,
        )
        logger.info(
            "[Telemetry] Итог цикла: outcome=%s trace_id=%s",
            final_outcome,
            trace_id,
        )
        _notify_outcome_if_needed(final_outcome, trace_id, summary)
    except Exception as exc:
        orchestrator_telemetry.end_cycle(
            trace_id,
            status="error",
            cycle_outcome=cs.ERROR,
            cycle_summary=cs.summarize_cycle(trace_id, cycle_num, cs.ERROR),
        )
        if os.getenv("ORC_ALERT_ON_BAD_OUTCOME", "true").lower() == "true":
            notifier.send_message(
                f"🔴 <b>Orchestrator — ошибка цикла</b>\n"
                f"trace: <code>{trace_id}</code>\n"
                f"<pre>{str(exc)[:800]}</pre>"
            )
        raise
