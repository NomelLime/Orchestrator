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
from datetime import datetime
from typing import Any, Dict, List, Optional

import config
from db.experiences import (
    save_evolution_plan, get_rich_experience_context, get_failed_patterns
)
from db.zones       import get_all_zones
from db.commands    import get_all_policies
from integrations.ollama_client import call_llm

logger = logging.getLogger(__name__)


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

    active_zones = [name for name, z in zones.items() if z.get("enabled")]

    # ── Секция метрик ─────────────────────────────────────────────────────────
    metrics_block = f"""
=== МЕТРИКИ ShortsProject (за {sp.get('period_hours', 24)} ч) ===
Просмотры: {sp.get('total_views', 0):,}
Лайки: {sp.get('total_likes', 0):,}
Средний CTR: {f"{sp.get('avg_ctr', 0):.3f}" if sp.get('avg_ctr') else 'нет данных'}
Топ платформа: {sp.get('top_platform') or 'нет данных'}
Бан-события: {sp.get('ban_count', 0)}
Статусы агентов: {json.dumps(sp.get('agent_statuses', {}), ensure_ascii=False)}

=== МЕТРИКИ PreLend (за {pl.get('period_hours', 24)} ч) ===
Кликов: {pl.get('total_clicks', 0):,}
Конверсий: {pl.get('conversions', 0):,}
CR: {f"{pl.get('cr', 0):.4f}" if pl.get('cr') else 'нет данных'}
% ботов: {f"{pl.get('bot_pct', 0):.1f}%" if pl.get('bot_pct') is not None else 'нет данных'}
Топ ГЕО: {pl.get('top_geo') or 'нет данных'}
Подозрения на шейв: {pl.get('shave_suspects', [])}
"""

    # ── Рекомендации Strategist SP ────────────────────────────────────────────
    # Strategist уже проанализировал данные SP 6 часов назад — используем его
    # выводы как дополнительный контекст, не дублируем GPU-нагрузку.
    strategist_recs = sp.get("strategist_recs", {})
    if strategist_recs:
        strategist_block = "\n=== РЕКОМЕНДАЦИИ ВНУТРЕННЕГО СТРАТЕГИСТА SP (последние 6ч) ===\n"
        strategist_block += "(можешь опираться на них или переопределить, если метрики говорят иное)\n"
        for agent_key, rec in strategist_recs.items():
            strategist_block += f"  {agent_key}: {str(rec)[:200]}\n"
    else:
        strategist_block = ""

    # ── Секция зон ────────────────────────────────────────────────────────────
    zones_block = "=== ДОСТУПНЫЕ ЗОНЫ ===\n"
    for name in ("scheduling", "visual", "prelend", "code"):
        z      = zones.get(name, {})
        status = "✅ АКТИВНА" if z.get("enabled") else "⛔ НЕАКТИВНА"
        zones_block += f"  {name}: {status} (confidence={z.get('confidence_score', 0)})\n"

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
        f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        + metrics_block
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
    """
    Извлекает JSON из ответа LLM.
    Устойчив к markdown-обёрткам и мусору до/после JSON.
    """
    # Убираем ```json ... ``` обёртки
    clean = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

    # Ищем первый {...} блок — сбалансированный поиск с учётом строкового контекста.
    # Скобки внутри JSON-строк ("...{...}...") не влияют на счётчик глубины.
    start = clean.find("{")
    if start == -1:
        return None

    depth   = 0
    in_str  = False
    escaped = False
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
                try:
                    return json.loads(clean[start:i+1])
                except json.JSONDecodeError as exc:
                    logger.warning("[Evolution] JSON parse error: %s", exc)
                    return None

    return None


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
