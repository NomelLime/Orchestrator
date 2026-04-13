"""
modules/tracking.py — Сбор данных из ShortsProject и PreLend.

Читает (ТОЛЬКО читает, никогда не пишет в управляемые проекты):
    ShortsProject:
        - data/analytics.json   → views, likes, comments, A/B данные
        - data/agent_memory.json → статусы агентов, события банов
    PreLend:
        - data/clicks.db        → клики, конверсии, GEO, боты
        - data/agent_memory.json → вердикты ANALYST
        - data/shave_report.json → коэффициенты шейва

Экспортирует:
    collect_shorts_project_snapshot() → dict с метриками SP
    collect_prelend_snapshot()        → dict с метриками PreLend
    collect_all_and_save()            → собирает оба снапшота и пишет в БД
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import config
from db.metrics import save_snapshot

logger = logging.getLogger(__name__)

_PLATFORM_ALIASES = {
    "youtube": "vk",
    "tiktok": "ok",
    "instagram": "ok",
    "vk_video": "vk",
    "odnoklassniki": "ok",
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_read_json(path: Path) -> Optional[Dict]:
    """Читает JSON-файл без исключений. Возвращает None при любой ошибке."""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[Tracking] Не удалось прочитать %s: %s", path, exc)
    return None


def _summarize_agent_statuses(agent_statuses: Dict[str, Any]) -> Dict[str, Any]:
    """Агрегирует статусы агентов в компактные health-метрики."""
    counts = Counter()
    for raw in (agent_statuses or {}).values():
        status = str(raw or "").strip().upper()
        if status in {"RUNNING", "ACTIVE", "OK"}:
            counts["running"] += 1
        elif status in {"IDLE", "SLEEP", "WAITING"}:
            counts["idle"] += 1
        elif status in {"ERROR", "FAILED", "CRASHED"}:
            counts["error"] += 1
        else:
            counts["other"] += 1
    total = sum(counts.values())
    return {
        "total": total,
        "running": counts["running"],
        "idle": counts["idle"],
        "error": counts["error"],
        "other": counts["other"],
        "running_ratio": (counts["running"] / total) if total > 0 else None,
    }


def _safe_primitive(value: Any) -> Any:
    """Возвращает JSON-совместимый scalar; иначе None."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return None


# ─────────────────────────────────────────────────────────────────────────────
# ShortsProject
# ─────────────────────────────────────────────────────────────────────────────

def collect_shorts_project_snapshot(period_hours: int = 24) -> Dict[str, Any]:
    """
    Читает analytics.json и agent_memory.json ShortsProject.
    Агрегирует метрики за последние period_hours часов.

    Возвращает dict:
        total_views, total_likes, avg_ctr, top_platform,
        ab_summary, ban_count, agent_statuses,
        raw_uploads (список последних загрузок для LLM-анализа)
    """
    result: Dict[str, Any] = {
        "period_hours":    period_hours,
        "total_views":     0,
        "total_likes":     0,
        "avg_ctr":         None,
        "top_platform":    None,
        "ab_summary":      [],
        "ban_count":       0,
        "agent_statuses":  {},
        "agent_health":    {
            "total": 0,
            "running": 0,
            "idle": 0,
            "error": 0,
            "other": 0,
            "running_ratio": None,
        },
        "raw_uploads":     [],
        "strategist_recs": {},  # рекомендации Strategist из agent_memory KV
        "strategist_recs_count": 0,
    }

    # ── analytics.json ───────────────────────────────────────────────────────
    analytics = _safe_read_json(config.SP_ANALYTICS_FILE)
    if analytics:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=period_hours)
        platform_views: Dict[str, int] = {}
        ctr_values: List[float] = []
        recent_uploads: List[Dict] = []

        for stem, entry in analytics.items():
            if not isinstance(entry, dict):
                continue
            for platform, upload in entry.get("uploads", {}).items():
                if not isinstance(upload, dict):
                    continue

                # Фильтруем по периоду
                uploaded_at_str = upload.get("uploaded_at")
                if uploaded_at_str:
                    try:
                        uploaded_dt = datetime.fromisoformat(uploaded_at_str)
                        # Нормализуем: если naive — считаем UTC
                        if uploaded_dt.tzinfo is None:
                            uploaded_dt = uploaded_dt.replace(tzinfo=timezone.utc)
                        if uploaded_dt < cutoff:
                            continue
                    except (ValueError, TypeError):
                        pass  # невалидная дата — не фильтруем

                views    = upload.get("views") or 0
                likes    = upload.get("likes") or 0
                comments = upload.get("comments") or 0
                platform_norm = _PLATFORM_ALIASES.get(str(platform).strip().lower(), str(platform).strip().lower())

                result["total_views"] += views
                result["total_likes"] += likes
                platform_views[platform_norm] = platform_views.get(platform_norm, 0) + views

                # CTR: (likes + comments) / views — простая прокси-метрика
                if views > 0:
                    ctr = (likes + comments) / views
                    ctr_values.append(ctr)

                recent_uploads.append({
                    "stem":       stem,
                    "platform":   platform_norm,
                    "views":      views,
                    "likes":      likes,
                    "ab_variant": upload.get("ab_variant"),
                    "uploaded_at": uploaded_at_str,
                })

            # A/B сводка
            ab_test = entry.get("ab_test")
            if ab_test:
                # TODO: когда будет достаточно данных — здесь вычислять победителя
                result["ab_summary"].append({
                    "stem":     stem,
                    "variants": list(ab_test.keys()),
                })

        if platform_views:
            result["top_platform"] = max(platform_views, key=platform_views.get)
        if ctr_values:
            result["avg_ctr"] = sum(ctr_values) / len(ctr_values)

        # Топ 20 загрузок для LLM
        result["raw_uploads"] = sorted(
            recent_uploads, key=lambda x: x.get("views") or 0, reverse=True
        )[:20]

    # ── agent_memory.json ────────────────────────────────────────────────────
    memory = _safe_read_json(config.SP_AGENT_MEMORY)
    if memory:
        result["agent_statuses"] = memory.get("agents", {})
        result["agent_health"] = _summarize_agent_statuses(result["agent_statuses"])

        kv = memory.get("kv", {})

        # Считаем бан-события из KV (Guardian логирует бан-сигналы)
        # Точный префикс "ban_" или ключ "ban" — избегаем ложных срабатываний на "banner", "bandwidth"
        ban_keys = [k for k in kv if k.lower().startswith("ban_") or k.lower() == "ban"]
        result["ban_count"] = len(ban_keys)

        # Рекомендации Strategist: ключи вида "rec.strategist.<agent>"
        # Strategist пишет их каждые 6 часов после Ollama-анализа.
        # Orchestrator читает эти выводы как входные данные — не дублирует анализ.
        strategist_recs = {
            k[len("rec.strategist."):]: v
            for k, v in kv.items()
            if k.startswith("rec.strategist.")
        }
        result["strategist_recs"] = strategist_recs
        result["strategist_recs_count"] = len(strategist_recs)
        if strategist_recs:
            logger.debug("[Tracking] Strategist рекомендации: %s", list(strategist_recs.keys()))

    logger.info(
        "[Tracking] SP: views=%d, likes=%d, CTR=%.3f, top_platform=%s, bans=%d, strategist_keys=%d",
        result["total_views"], result["total_likes"],
        result["avg_ctr"] or 0,
        result["top_platform"] or "?",
        result["ban_count"],
        len(result["strategist_recs"]),
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# PreLend
# ─────────────────────────────────────────────────────────────────────────────

def collect_prelend_snapshot(period_hours: int = 24) -> Dict[str, Any]:
    """
    Получает метрики PreLend через Internal API (HTTP).
    PreLend находится на VPS — прямой доступ к файлам недоступен.

    Возвращает dict:
        total_clicks, conversions, cr, bot_pct, top_geo,
        shave_suspects, analyst_verdicts
    """
    _empty: Dict[str, Any] = {
        "period_hours":       period_hours,
        "total_clicks":       0,
        "conversions":        0,
        "cr":                 None,
        "bot_pct":            None,
        "top_geo":            None,
        "shave_suspects":     [],
        "analyst_verdicts":   {},
        "analyst_verdicts_count": 0,
        "hook_metrics": {},
        "risk_metrics": {},
        "traffic_alive":      None,
        "last_click_ago_sec": None,
        "_unreachable":       True,
    }

    from integrations.prelend_client import get_client
    client = get_client()

    if not client.is_available():
        logger.warning(
            "[Tracking] PreLend Internal API недоступен (%s). "
            "PL метрики обнулены. Проверьте SSH tunnel / WireGuard.",
            client.base_url,
        )
        return _empty

    data = client.get_metrics(period_hours=period_hours)
    hook_data = client.get_hook_metrics(period_hours=max(24, period_hours * 3))
    risk_data = client.get_risk_metrics(period_hours=max(24, period_hours * 3))
    if not data or data.get("error") == "unreachable":
        logger.warning("[Tracking] PreLend API вернул пустой ответ — PL метрики обнулены")
        return _empty

    # Запрашиваем /health для traffic_alive и last_click_ago_sec
    health = client.get_health() or {}

    # API возвращает данные уже в нужном формате
    result: Dict[str, Any] = {
        "period_hours":       period_hours,
        "total_clicks":       data.get("total_clicks", 0),
        "conversions":        data.get("conversions", 0),
        "cr":                 data.get("cr"),
        "bot_pct":            data.get("bot_pct"),
        "top_geo":            data.get("top_geo"),
        "shave_suspects":     [
            s["id"] if isinstance(s, dict) else s
            for s in data.get("shave_suspects", [])
        ],
        "analyst_verdicts":   data.get("analyst_verdicts", {}),
        "analyst_verdicts_count": len(data.get("analyst_verdicts", {}) or {}),
        "hook_metrics":       hook_data if isinstance(hook_data, dict) else {},
        "risk_metrics":       risk_data if isinstance(risk_data, dict) else {},
        "traffic_alive":      _safe_primitive(health.get("traffic_alive")),
        "last_click_ago_sec": _safe_primitive(health.get("last_click_ago_sec")),
        "_unreachable":       False,
    }

    logger.info(
        "[Tracking] PreLend: clicks=%d, CR=%.3f, bots=%.1f%%, geo=%s, traffic_alive=%s",
        result["total_clicks"],
        result["cr"] or 0,
        result["bot_pct"] or 0,
        result["top_geo"] or "?",
        result["traffic_alive"],
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Объединённый сбор
# ─────────────────────────────────────────────────────────────────────────────

def collect_all_and_save() -> Dict[str, Any]:
    """
    Собирает снапшоты из обоих проектов и сохраняет в metrics_snapshots.
    Возвращает dict с данными обоих снапшотов (передаётся в evolution.py).
    """
    sp = collect_shorts_project_snapshot()
    pl = collect_prelend_snapshot()

    # Сохраняем в БД
    save_snapshot(
        source          = "ShortsProject",
        period_hours    = sp["period_hours"],
        sp_total_views  = sp["total_views"],
        sp_total_likes  = sp["total_likes"],
        sp_avg_ctr      = sp["avg_ctr"],
        sp_top_platform = sp["top_platform"],
        sp_ban_count    = sp["ban_count"],
        raw_summary     = {
            "ab_summary":     sp["ab_summary"][:5],
            "agent_statuses": sp["agent_statuses"],
            "agent_health":   sp["agent_health"],
            "strategist_recs_count": sp["strategist_recs_count"],
            "top_uploads":    sp["raw_uploads"][:5],
            "_analytics_available": bool(sp["total_views"] or sp["total_likes"] or sp["raw_uploads"]),
        },
    )

    save_snapshot(
        source          = "PreLend",
        period_hours    = pl["period_hours"],
        pl_total_clicks = pl["total_clicks"],
        pl_conversions  = pl["conversions"],
        pl_cr           = pl["cr"],
        pl_bot_pct      = pl["bot_pct"],
        pl_top_geo      = pl["top_geo"],
        raw_summary     = {
            "shave_suspects":  pl["shave_suspects"],
            "analyst_verdicts": pl["analyst_verdicts"],
            "analyst_verdicts_count": pl["analyst_verdicts_count"],
            "traffic_alive": pl["traffic_alive"],
            "last_click_ago_sec": pl["last_click_ago_sec"],
            "_unreachable": pl.get("_unreachable", False),
        },
    )

    return {"shorts_project": sp, "prelend": pl}
