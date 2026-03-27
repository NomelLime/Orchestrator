"""
modules/policies.py — Интерпретация и применение операторских команд.

Читает pending команды из БД (от Telegram), вызывает LLM для интерпретации,
записывает политики и передаёт в modules/zones.py.

Экспортирует:
    process_pending_commands() → количество обработанных команд
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, TypedDict

import config
from db.commands import (
    get_pending_commands, save_command,
    mark_command_applied, mark_command_rejected,
    set_policy, get_all_policies,
)
from integrations.ollama_client import call_llm
from langgraph.graph import END, StateGraph
from modules import cycle_semantics as cs
from modules import orchestrator_telemetry

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Промпт для интерпретации команды оператора
# ─────────────────────────────────────────────────────────────────────────────

_PARSE_CMD_PROMPT = """Ты — парсер команд оператора системы автоматизации.
Пользователь пишет на свободном языке, ты должен интерпретировать и вернуть JSON.

Доступные типы команд:
  policy_update: включить/выключить/заморозить/разморозить зону,
                 сменить режим (safe/aggressive), установить фокус GEO
  manual_action: откатить последний план, поставить на паузу, запустить цикл вручную
  config_hint:   мягкое указание ('осторожнее', 'не трогай X', 'фокус на BR')

Доступные зоны: scheduling, visual, prelend, code

Сообщение оператора: "{message}"

Верни ТОЛЬКО JSON (без markdown):
{{
  "type": "policy_update|manual_action|config_hint",
  "action": "конкретное действие",
  "zone": "имя зоны или null",
  "params": {{...}}  // дополнительные параметры
}}
"""


class PolicyCommandState(TypedDict, total=False):
    command_id: int
    raw_text: str
    trace_id: str
    parsed: Dict[str, Any]
    rejected: bool
    reject_reason: str
    applied: bool
    outcome: str


def process_pending_commands(trace_id: str = "") -> int:
    """
    Обрабатывает все pending-команды от оператора.
    Каждую команду:
        1. Интерпретирует через LLM
        2. Записывает parsed_json в БД
        3. Применяет к политикам/зонам
        4. Помечает как applied/rejected

    trace_id — корреляция с циклом Orchestrator (телеметрия policy_command_trace.jsonl).
    """
    commands = get_pending_commands()
    if not commands:
        return 0

    from modules.zones import process_zone_commands
    applied = 0

    graph = _get_policy_command_graph()
    for cmd in commands:
        result = graph.invoke(
            {
                "command_id": int(cmd["id"]),
                "raw_text": str(cmd["raw_text"]),
                "trace_id": trace_id,
            }
        )
        if result.get("applied"):
            applied += 1

    # После обновления parsed_json — запускаем обработчик зон
    process_zone_commands()

    return applied


def _node_parse(state: PolicyCommandState) -> PolicyCommandState:
    command_id = state["command_id"]
    raw_text = (state.get("raw_text") or "").strip()
    tid = state.get("trace_id") or ""
    logger.info("[Policies] Обработка команды #%d: %s", command_id, raw_text[:60])

    if not raw_text:
        orchestrator_telemetry.append_policy_command_event(
            tid, command_id, "parse", cs.NEEDS_CLARIFICATION, {"reason": "empty_message"}
        )
        return {
            "rejected": True,
            "reject_reason": "Пустое сообщение",
            "outcome": cs.NEEDS_CLARIFICATION,
        }

    parsed = _parse_command_with_llm(raw_text)
    if not parsed:
        logger.warning("[Policies] Не удалось интерпретировать команду #%d", command_id)
        orchestrator_telemetry.append_policy_command_event(
            tid, command_id, "parse", cs.LLM_EMPTY, {"reason": "llm_unparseable"}
        )
        return {
            "rejected": True,
            "reject_reason": "LLM не смогла интерпретировать",
            "outcome": cs.LLM_EMPTY,
        }

    orchestrator_telemetry.append_policy_command_event(
        tid,
        command_id,
        "parse",
        cs.OK,
        {"type": parsed.get("type"), "action": parsed.get("action")},
    )
    return {"parsed": parsed, "rejected": False, "outcome": cs.OK}


def _node_reject(state: PolicyCommandState) -> PolicyCommandState:
    mark_command_rejected(state["command_id"], state.get("reject_reason") or "Ошибка парсинга")
    tid = state.get("trace_id") or ""
    oc = state.get("outcome") or cs.POLICY_GAP
    orchestrator_telemetry.append_policy_command_event(
        tid,
        state["command_id"],
        "rejected",
        oc,
        {"reason": state.get("reject_reason")},
    )
    return {"applied": False}


def _node_persist_parsed(state: PolicyCommandState) -> PolicyCommandState:
    parsed = state["parsed"]
    from db.connection import get_db

    with get_db() as conn:
        conn.execute(
            "UPDATE operator_commands SET parsed_json = ?, command_type = ? WHERE id = ?",
            (json.dumps(parsed, ensure_ascii=False), parsed.get("type"), state["command_id"]),
        )
    return {}


def _node_apply(state: PolicyCommandState) -> PolicyCommandState:
    _apply_parsed_command(state["parsed"], state["command_id"])
    return {}


def _node_mark_applied(state: PolicyCommandState) -> PolicyCommandState:
    mark_command_applied(state["command_id"])
    tid = state.get("trace_id") or ""
    orchestrator_telemetry.append_policy_command_event(
        tid,
        state["command_id"],
        "applied",
        cs.OK,
        {"type": (state.get("parsed") or {}).get("type")},
    )
    return {"applied": True}


def _route_after_parse(state: PolicyCommandState) -> str:
    return "reject" if state.get("rejected") else "persist"


def _build_policy_command_graph():
    graph = StateGraph(PolicyCommandState)
    graph.add_node("parse", _node_parse)
    graph.add_node("reject", _node_reject)
    graph.add_node("persist", _node_persist_parsed)
    graph.add_node("apply", _node_apply)
    graph.add_node("mark_applied", _node_mark_applied)
    graph.set_entry_point("parse")
    graph.add_conditional_edges(
        "parse",
        _route_after_parse,
        {"reject": "reject", "persist": "persist"},
    )
    graph.add_edge("reject", END)
    graph.add_edge("persist", "apply")
    graph.add_edge("apply", "mark_applied")
    graph.add_edge("mark_applied", END)
    return graph.compile()


_POLICY_COMMAND_GRAPH = None


def _get_policy_command_graph():
    global _POLICY_COMMAND_GRAPH
    if _POLICY_COMMAND_GRAPH is None:
        _POLICY_COMMAND_GRAPH = _build_policy_command_graph()
    return _POLICY_COMMAND_GRAPH


def _parse_command_with_llm(raw_text: str) -> Optional[Dict]:
    """Отправляет команду в LLM для структурированной интерпретации."""
    prompt = _PARSE_CMD_PROMPT.format(message=raw_text)
    raw    = call_llm(model=config.OLLAMA_STRATEGY_MODEL, prompt=prompt)
    if not raw:
        return None

    import re
    clean = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

    # Balanced-brace parser с учётом строкового контекста.
    # Скобки внутри JSON-строк ("...{...}...") не влияют на счётчик глубины.
    start = clean.find("{")
    if start == -1:
        return None
    depth    = 0
    in_str   = False
    escaped  = False
    end      = start
    for i, ch in enumerate(clean[start:], start):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_str:
            escaped = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    else:
        return None  # незакрытый объект

    try:
        return json.loads(clean[start:end + 1])
    except json.JSONDecodeError:
        return None


def _apply_parsed_command(parsed: Dict, command_id: int) -> None:
    """Применяет интерпретированную команду к политикам."""
    cmd_type = parsed.get("type")
    action   = parsed.get("action", "")
    params   = parsed.get("params", {})

    if cmd_type == "policy_update":
        zone = parsed.get("zone")

        if action in ("freeze_zone", "disable_zone") and zone:
            set_policy(f"freeze_zone_{zone}", True, command_id=command_id,
                       description=f"оператор: заморозка {zone}")

        elif action in ("unfreeze_zone", "enable_zone") and zone:
            set_policy(f"freeze_zone_{zone}", False, command_id=command_id,
                       description=f"оператор: разморозка {zone}")

        elif action == "set_mode":
            mode = params.get("mode", "safe")
            set_policy("mode", mode, command_id=command_id,
                       description=f"оператор: режим {mode}")

        elif action == "focus_geo":
            geo = params.get("geo") or params.get("value")
            if geo:
                set_policy("focus_geo", geo, command_id=command_id,
                           description=f"оператор: фокус на {geo}")

    elif cmd_type == "manual_action":
        if action == "pause_evolution":
            set_policy("pause_evolution", True, command_id=command_id,
                       description="оператор: пауза эволюции")

        elif action == "resume_evolution":
            set_policy("pause_evolution", False, command_id=command_id)

        elif action == "rollback_last":
            from db.experiences import get_last_applied_plan_id
            from db.commands import set_policy as _set_policy
            last_plan_id = get_last_applied_plan_id()
            if last_plan_id:
                # Откат через git revert: переиспользуем механизм code_evolver
                from integrations import git_tools
                commit_hash = git_tools.find_last_orc_commit(config.SHORTS_PROJECT_DIR)
                if commit_hash:
                    reverted = git_tools.revert_commit(config.SHORTS_PROJECT_DIR, commit_hash)
                    if reverted:
                        logger.info(
                            "[Policies] Откат плана #%d: revert коммита %s",
                            last_plan_id, commit_hash,
                        )
                    else:
                        logger.error("[Policies] git revert %s не удался", commit_hash)
                else:
                    logger.warning("[Policies] Нет Orchestrator-коммитов для отката плана #%d", last_plan_id)
            else:
                logger.warning("[Policies] Нет применённых планов для отката")

        elif action == "trigger_cycle":
            from main_orchestrator import trigger_force_cycle
            trigger_force_cycle()
            set_policy("force_cycle", True, command_id=command_id,
                       description="оператор: внеочередной цикл")

    elif cmd_type == "config_hint":
        # Мягкие указания сохраняем как политику для LLM-промпта
        hint_key = f"hint_{action}" if action else f"hint_{command_id}"
        set_policy(hint_key, params.get("value") or action,
                   command_id=command_id,
                   description=f"оператор: подсказка — {action}")

    logger.info("[Policies] Команда применена: type=%s action=%s", cmd_type, action)
