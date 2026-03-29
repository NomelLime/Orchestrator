"""
modules/evolution.py — EvolutionEngine: LLM-анализ и генерация плана.

Принимает данные из tracking.py, строит промпт, отправляет в Ollama,
парсит JSON-план и сохраняет в evolution_plans.

Экспортирует:
    generate_plan(data) → dict плана или None если LLM вернула мусор
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config
from db.experiences import (
    save_evolution_plan,
    get_rich_experience_context,
    get_failed_patterns,
    get_recent_plan_scores,
    get_avg_llm_judge_stats,
)
from db.zones       import get_all_zones
from db.commands    import get_all_policies
from modules.financial_observer import get_financial_context
from modules.code_evolver import sanitize_for_prompt as _san
from integrations.ollama_client import call_llm


def _safe_finances_block(finances: dict) -> str:
    """
    Формирует finances_block из get_financial_context() с санитизацией строк (FIX#V3-5).

    Числовые поля (net_roi_rub, roi_pct и др.) безопасны.
    Строковые поля и ключи словарей by_source санитизируются через sanitize_for_prompt.

    Args:
        finances: dict от get_financial_context()

    Returns:
        Отформатированная строка для LLM-промпта или "" если finances пустой.
    """
    if not finances:
        return ""

    lines = ["\n=== ФИНАНСЫ (последние 30 дней) ==="]
    for key, value in finances.items():
        if isinstance(value, str):
            value = _san(value, 200)
        elif isinstance(value, dict):
            # by_source — вложенный dict с названиями источников (внешние данные)
            safe_dict = {}
            for k, v in value.items():
                safe_k = _san(str(k), 50)
                safe_v = _san(str(v), 100) if isinstance(v, str) else v
                safe_dict[safe_k] = safe_v
            value = safe_dict
        # Числовые значения (int/float) не требуют санитизации
        lines.append(f"  {key}: {value}")
    return "\n".join(lines) + "\n"


logger = logging.getLogger(__name__)


def _should_defer_llm() -> bool:
    """True если ShortsProject в этапе с тяжёлой VL нагрузкой — отложить стратегический LLM."""
    state_file = config.SHORTS_PROJECT_DIR / "data" / "pipeline_state.json"
    try:
        st = json.loads(state_file.read_text(encoding="utf-8"))
        cur = st.get("current_stage")
        if cur in ("processing", "search", "upload"):
            return True
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Главная функция
# ─────────────────────────────────────────────────────────────────────────────

def generate_plan(metrics_data: Dict[str, Any]) -> Optional[Dict]:
    """
    Запускает LLM-анализ и генерирует план эволюции.

    Args:
        metrics_data: результат collect_all_and_save() из tracking.py

    Returns:
        dict плана (в формате JSON-схемы из промпта) или None при ошибке.
    """
    # [FIX] Не блокируем основной цикл на 5 мин. Возвращаем deferred-индикатор,
    # main_orchestrator обработает это (повторит попытку в следующем цикле).
    if _should_defer_llm():
        logger.info("[Evolution] SP pipeline в VL-этапе — откладываем LLM-генерацию")
        return {"_deferred": True, "_reason": "SP VL stage active"}

    prompt = _build_prompt(metrics_data)
    logger.info("[Evolution] Запрос к LLM (модель: %s)", config.OLLAMA_STRATEGY_MODEL)

    raw_response = call_llm(
        model  = config.OLLAMA_STRATEGY_MODEL,
        prompt = prompt,
    )
    if not raw_response:
        logger.warning("[Evolution] LLM вернула пустой ответ")
        return None

    plan = _parse_plan(raw_response)
    if not plan:
        logger.warning("[Evolution] Не удалось распарсить план из ответа LLM")
        return None

    # Определяем затронутые зоны из плана
    zones_affected = list(plan.get("targets", {}).get("zones", []))
    files_affected = _extract_files(plan)
    risk_level     = plan.get("risk_assessment", {}).get("estimated_risk", "low")
    summary        = plan.get("summary", "Без описания")

    plan_id = save_evolution_plan(
        summary        = summary,
        raw_plan       = plan,
        zones_affected = zones_affected,
        files_affected = files_affected,
        risk_level     = risk_level,
    )
    plan["_plan_id"] = plan_id

    logger.info("[Evolution] План #%d сгенерирован: %s (зоны: %s, риск: %s)",
                plan_id, summary[:60], zones_affected, risk_level)
    return plan


# ─────────────────────────────────────────────────────────────────────────────
# Построение промпта
# ─────────────────────────────────────────────────────────────────────────────

def _build_prompt(metrics_data: Dict) -> str:
    """
    Строит системный промпт для LLM.

    Структура:
        1. Роль и цель
        2. Текущие метрики (ShortsProject + PreLend)
        3. Состояние зон (какие активны)
        4. Прошлый опыт (успешные и неудачные изменения)
        5. Активные политики оператора
        6. Инструкции по формату ответа (JSON)
    """
    sp = metrics_data.get("shorts_project", {})
    pl = metrics_data.get("prelend", {})

    zones    = get_all_zones()
    policies = get_all_policies()
    recent   = get_rich_experience_context(last_n=10)
    failed   = get_failed_patterns()

    finances  = get_financial_context(days=30)
    active_zones = [name for name, z in zones.items() if z.get("enabled")]
    # Формируем finances_block с санитизацией (FIX#V3-5)
    finances_block = _safe_finances_block(finances)

    # ── Секция метрик ─────────────────────────────────────────────────────────
    # Санитизируем внешние данные перед вставкой в LLM-промпт (защита от prompt-injection)
    _raw_statuses = sp.get("agent_statuses", {})
    _safe_statuses = {
        _san(str(k), 50): _san(str(v), 200)
        for k, v in _raw_statuses.items()
    }
    _raw_suspects = pl.get("shave_suspects", [])
    _safe_suspects = [_san(str(s), 100) for s in _raw_suspects[:10]]

    metrics_block = f"""
=== МЕТРИКИ ShortsProject (за {sp.get('period_hours', 24)} ч) ===
Просмотры: {sp.get('total_views', 0):,}
Лайки: {sp.get('total_likes', 0):,}
Средний CTR: {f"{sp.get('avg_ctr', 0):.3f}" if sp.get('avg_ctr') else 'нет данных'}
Топ платформа: {_san(str(sp.get('top_platform') or 'нет данных'), 50)}
Бан-события: {sp.get('ban_count', 0)}
Статусы агентов: {json.dumps(_safe_statuses, ensure_ascii=False)}

=== МЕТРИКИ PreLend (за {pl.get('period_hours', 24)} ч) ===
Кликов: {pl.get('total_clicks', 0):,}
Конверсий: {pl.get('conversions', 0):,}
CR: {f"{pl.get('cr', 0):.4f}" if pl.get('cr') else 'нет данных'}
% ботов: {f"{pl.get('bot_pct', 0):.1f}%" if pl.get('bot_pct') is not None else 'нет данных'}
Топ ГЕО: {_san(str(pl.get('top_geo') or 'нет данных'), 10)}
Подозрения на шейв: {_safe_suspects}
"""

    # ── Качество последних планов ─────────────────────────────────────────────
    recent_scores = get_recent_plan_scores(limit=5)
    if recent_scores:
        quality_block = "\n=== КАЧЕСТВО ПОСЛЕДНИХ ПЛАНОВ (оценка через 24ч) ===\n"
        for s in recent_scores:
            line = f"  План #{s['plan_id']}: score={s['overall_score']:+.1f}"
            if s.get("llm_judge_score") is not None:
                line += f", LLM={s['llm_judge_score']}/10"
            if s.get('views_delta_pct') is not None:
                line += f", views={s['views_delta_pct']:+.1f}%"
            if s.get("ctr_delta_pct") is not None:
                line += f", CTR={s['ctr_delta_pct']:+.1f}%"
            if s.get("cr_delta_pct") is not None:
                line += f", CR={s['cr_delta_pct']:+.1f}%"
            quality_block += line + "\n"
        avg_llm, n_llm = get_avg_llm_judge_stats()
        if n_llm and avg_llm is not None:
            quality_block += f"\n  Средний LLM-as-judge по прошлым планам: {avg_llm:.1f} (из {n_llm} оценок)\n"
        else:
            quality_block += "\n  Средний LLM-as-judge: N/A (пока нет оценок)\n"
    else:
        quality_block = ""
        avg_llm, n_llm = get_avg_llm_judge_stats()
        if n_llm and avg_llm is not None:
            quality_block = (
                "\n=== КАЧЕСТВО ПЛАНОВ (LLM-as-judge) ===\n"
                f"  Средний score: {avg_llm:.1f} (из {n_llm} оценок)\n"
            )

    # ── Рекомендации Strategist SP ────────────────────────────────────────────
    # Strategist уже проанализировал данные SP 6 часов назад — используем его
    # выводы как дополнительный контекст, не дублируем GPU-нагрузку.
    strategist_recs = sp.get("strategist_recs", {})
    if strategist_recs:
        strategist_block = "\n=== РЕКОМЕНДАЦИИ ВНУТРЕННЕГО СТРАТЕГИСТА SP (последние 6ч) ===\n"
        strategist_block += "(можешь опираться на них или переопределить, если метрики говорят иное)\n"
        for agent_key, rec in strategist_recs.items():
            strategist_block += f"  {_san(str(agent_key), 100)}: {_san(str(rec), 500)}\n"
    else:
        strategist_block = ""

    # ── Секция зон ────────────────────────────────────────────────────────────
    _zone_hints = {
        "scheduling": "upload_schedule в config.json аккаунтов",
        "visual":     "visual_filter в config.json аккаунтов (см. пример в формате ниже)",
        "prelend":    "settings.json и advertisers.json (пороги алертов, ставки)",
        "code":       "Python-файлы ShortsProject (требуют /approve от оператора)",
    }
    zones_block = "=== ДОСТУПНЫЕ ЗОНЫ ===\n"
    for name in ("scheduling", "visual", "prelend", "code"):
        z      = zones.get(name, {})
        status = "✅ АКТИВНА" if z.get("enabled") else "⛔ НЕАКТИВНА"
        hint   = _zone_hints.get(name, "")
        zones_block += f"  {name}: {status} (confidence={z.get('confidence_score', 0)}) — {hint}\n"

    # ── Секция опыта с реальными результатами ─────────────────────────────────
    experience_block = "=== ПРОШЛЫЕ ЭКСПЕРИМЕНТЫ И ИХ РЕЗУЛЬТАТЫ ===\n"
    experience_block += "(формат: статус [зона] описание → результат через 24ч)\n"
    if recent:
        for exp in recent[:8]:
            if exp.get("rolled_back"):
                icon = "❌"
                outcome = f"откат: {exp.get('rollback_reason') or 'тесты упали'}"
            elif exp.get("metric_impact"):
                impact  = exp["metric_impact"]
                parts   = []
                if "views_delta_pct" in impact:
                    parts.append(f"views {impact['views_delta_pct']:+.1f}%")
                if "ctr_delta_pct" in impact:
                    parts.append(f"CTR {impact['ctr_delta_pct']:+.1f}%")
                if "cr_delta_pct" in impact:
                    parts.append(f"CR {impact['cr_delta_pct']:+.1f}%")
                if "ban_delta" in impact:
                    parts.append(f"баны {impact['ban_delta']:+d}")
                if "bot_pct_delta" in impact:
                    parts.append(f"боты {impact['bot_pct_delta']:+.1f}%")
                outcome = ", ".join(parts) if parts else "данные есть, дельта 0"
                icon    = "✅" if any(
                    impact.get(k, 0) > 0 for k in ("views_delta_pct", "ctr_delta_pct", "cr_delta_pct")
                ) else "⚠️"
            else:
                icon    = "⏳"
                outcome = "оценка ещё не готова (< 24ч)"
            experience_block += (
                f"  {icon} [{exp['zone']}] {exp['description'][:75]} → {outcome}\n"
            )
    else:
        experience_block += "  Нет данных (первый запуск)\n"

    if failed:
        experience_block += "\n=== НЕ ПОВТОРЯТЬ (привели к откату / ухудшению) ===\n"
        for f in failed[:5]:
            experience_block += f"  ❌ {f}\n"

    # ── Секция политик ─────────────────────────────────────────────────────────
    policies_block = ""
    if policies:
        policies_block = "\n=== ИНСТРУКЦИИ ОПЕРАТОРА ===\n"
        for k, v in policies.items():
            policies_block += f"  {k}: {v}\n"

    # ── Инструкции по формату ─────────────────────────────────────────────────
    format_instructions = """
=== ТВОЯ РОЛЬ И ЦЕЛЬ ===
Ты — владелец digital-бизнеса, который управляет двумя активами:
  • ShortsProject — органический короткий видео-трафик (YouTube/TikTok/Instagram)
  • PreLend — монетизация трафика: клоакинг, конверсии, рекламные ставки

Твоя единственная цель — максимизировать ROI-скор:
  ROI = views_delta_pct × engagement_rate × survival_rate × account_health
  Где:
    views_delta_pct  — рост просмотров за последние 24ч (относительный %)
    engagement_rate  — (лайки + комментарии) / просмотры
    survival_rate    — 1 − (ban_count / active_accounts)
    account_health   — средний здоровый статус аккаунтов (0.0–1.0)

CTR и абсолютные просмотры — промежуточные индикаторы, не самоцель.
Баны = прямые потери ROI: меньше аккаунтов → меньше трафика → меньше конверсий.

МЕТОДОЛОГИЯ:
• Одна чёткая, обратимая гипотеза за цикл.
• Не менять несколько переменных одновременно.
• Опираться исключительно на задокументированные результаты прошлых экспериментов.
• Предпочитать config_changes перед code_patches — быстрее, обратимее, без рисков.

ПРАВИЛА ПРИНЯТИЯ РЕШЕНИЙ:
1. Изменения только в АКТИВНЫХ зонах (см. выше).
2. Не повторять эксперименты с отрицательным или нулевым результатом.
3. При росте бан-событий ≥ 2 — ТОЛЬКО снижать агрессию публикаций, ничего другого.
4. Zone 'code' — только Python-файлы ShortsProject. ВАЖНО: code_patches требуют
   ручного одобрения оператора в Telegram — предлагай только при высокой уверенности
   и когда config_change недостаточно.
5. Zone 'prelend' — только settings.json (пороги алертов) и advertisers.json (ставки).
6. Если нет чётких данных для улучшения — верни summary: "Пропуск цикла: данных недостаточно"
   и пустые targets.

Верни ТОЛЬКО валидный JSON без markdown, пояснений и преамбул:

{
  "plan_id": null,
  "created_at": "ISO-TIMESTAMP",
  "summary": "Краткое описание плана (1-2 предложения, на русском)",
  "targets": {
    "zones": ["scheduling"],
    "shorts_project": {
      "config_changes": [
        {
          "scope": "scheduling",
          "description": "что именно изменить и почему",
          "accounts": ["all"],
          "platform": "tiktok",
          "param": "upload_schedule",
          "new_value": ["20:00", "22:00"]
        },
        {
          "scope": "visual",
          "description": "применить cinematic фильтр — A/B тест визуального стиля для повышения retention",
          "accounts": ["all"],
          "param": "visual_filter",
          "new_value": "cinematic"
        }
      ],
      "code_patches": [
        {
          "file": "pipeline/agents/editor.py",
          "goal": "конкретная однострочная цель изменения",
          "justification": "Почему именно сейчас и почему лучше предыдущих попыток: ..."
        }
      ]
    },
    "prelend": {
      "config_changes": [
        {
          "scope": "thresholds",
          "description": "что именно изменить и почему",
          "param": "bot_pct_per_hour",
          "new_value": 30
        },
        {
          "scope": "advertiser_rate",
          "description": "снизить ставку underperforming рекламодателя",
          "advertiser_id": "adv_001",
          "new_value": 3.5
        }
      ],
      "code_patches": []
    }
  },
  "risk_assessment": {
    "estimated_risk": "low",
    "notes": "почему такой уровень риска и как он влияет на ROI-скор"
  }
}
"""

    return (
        f"Дата: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n\n"
        + metrics_block
        + finances_block
        + quality_block
        + strategist_block
        + zones_block
        + experience_block
        + policies_block
        + format_instructions
    )


# ─────────────────────────────────────────────────────────────────────────────
# Парсинг ответа LLM
# ─────────────────────────────────────────────────────────────────────────────

def _parse_plan(raw: str) -> Optional[Dict]:
    """Извлекает JSON-план из ответа LLM. Делегирует в utils/llm_json.py (DRY)."""
    from utils.llm_json import extract_json_object
    return extract_json_object(raw)


def _extract_files(plan: Dict) -> List[str]:
    """Собирает список файлов из плана для записи в БД."""
    files = []
    targets = plan.get("targets", {})
    for repo_key in ("shorts_project", "prelend"):
        repo = targets.get(repo_key, {})
        for patch in repo.get("code_patches", []):
            if "file" in patch:
                files.append(patch["file"])
    return files
