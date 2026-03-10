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
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import config
from db.metrics import save_snapshot

logger = logging.getLogger(__name__)


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
        "period_hours":  period_hours,
        "total_views":   0,
        "total_likes":   0,
        "avg_ctr":       None,
        "top_platform":  None,
        "ab_summary":    [],
        "ban_count":     0,
        "agent_statuses":{},
        "raw_uploads":   [],
    }

    # ── analytics.json ───────────────────────────────────────────────────────
    analytics = _safe_read_json(config.SP_ANALYTICS_FILE)
    if analytics:
        cutoff = datetime.now() - timedelta(hours=period_hours)
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
                        if datetime.fromisoformat(uploaded_at_str) < cutoff:
                            continue
                    except Exception:
                        pass

                views    = upload.get("views") or 0
                likes    = upload.get("likes") or 0
                comments = upload.get("comments") or 0

                result["total_views"] += views
                result["total_likes"] += likes
                platform_views[platform] = platform_views.get(platform, 0) + views

                # CTR: (likes + comments) / views — простая прокси-метрика
                if views > 0:
                    ctr = (likes + comments) / views
                    ctr_values.append(ctr)

                recent_uploads.append({
                    "stem":       stem,
                    "platform":   platform,
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

        # Считаем бан-события из KV (Guardian логирует бан-сигналы)
        # TODO: точный формат ban-событий уточнить после просмотра guardian.py
        kv = memory.get("kv", {})
        ban_keys = [k for k in kv if "ban" in k.lower()]
        result["ban_count"] = len(ban_keys)

    logger.info(
        "[Tracking] SP: views=%d, likes=%d, CTR=%.3f, top_platform=%s, bans=%d",
        result["total_views"], result["total_likes"],
        result["avg_ctr"] or 0,
        result["top_platform"] or "?",
        result["ban_count"],
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# PreLend
# ─────────────────────────────────────────────────────────────────────────────

def collect_prelend_snapshot(period_hours: int = 24) -> Dict[str, Any]:
    """
    Читает clicks.db (SQLite) и agent_memory.json PreLend.
    Агрегирует метрики за последние period_hours часов.

    Возвращает dict:
        total_clicks, conversions, cr, bot_pct, top_geo,
        shave_suspects, analyst_verdicts
    """
    result: Dict[str, Any] = {
        "period_hours":    period_hours,
        "total_clicks":    0,
        "conversions":     0,
        "cr":              None,
        "bot_pct":         None,
        "top_geo":         None,
        "shave_suspects":  [],
        "analyst_verdicts":{},
    }

    # ── clicks.db ────────────────────────────────────────────────────────────
    db_path = config.PL_CLICKS_DB
    if db_path.exists():
        try:
            since_ts = int(time.time()) - period_hours * 3600
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row

            # Общая статистика кликов из таблицы clicks
            # Статусы: 'sent' | 'converted' | 'bot' | 'cloaked'
            # 'cloaked' = off-geo трафик (перенаправлен на заглушку)
            row = conn.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'bot'     THEN 1 ELSE 0 END) AS bots,
                    SUM(CASE WHEN status = 'cloaked' THEN 1 ELSE 0 END) AS cloaked
                FROM clicks
                WHERE ts >= ? AND is_test = 0
            """, (since_ts,)).fetchone()

            if row and row["total"] > 0:
                result["total_clicks"] = row["total"]
                result["bot_pct"] = (row["bots"] or 0) / row["total"] * 100

            # Конверсии берём из отдельной таблицы conversions (более точный источник).
            # logApi() обновляет clicks.status='converted' И вставляет в conversions.
            # logManual() пишет только в conversions (без связки с кликом).
            # Поэтому conversions-таблица — единственный надёжный источник.
            conv_row = conn.execute("""
                SELECT COALESCE(SUM(count), 0) AS total_convs
                FROM conversions
                WHERE created_at >= ?
            """, (since_ts,)).fetchone()

            if conv_row:
                result["conversions"] = int(conv_row["total_convs"])
                if result["total_clicks"] > 0:
                    result["cr"] = result["conversions"] / result["total_clicks"]

            # Топ ГЕО по отправленным (не bot/cloaked) кликам
            geo_row = conn.execute("""
                SELECT geo, COUNT(*) AS cnt
                FROM clicks
                WHERE ts >= ? AND is_test = 0 AND status NOT IN ('bot', 'cloaked')
                GROUP BY geo ORDER BY cnt DESC LIMIT 1
            """, (since_ts,)).fetchone()
            if geo_row:
                result["top_geo"] = geo_row["geo"]

            conn.close()

        except Exception as exc:
            logger.warning("[Tracking] Ошибка чтения clicks.db: %s", exc)

    # ── agent_memory.json ────────────────────────────────────────────────────
    memory = _safe_read_json(config.PL_AGENT_MEMORY)
    if memory:
        kv = memory.get("kv", {})
        verdicts_data = kv.get("analyst_last_verdicts", {})
        result["analyst_verdicts"] = verdicts_data.get("verdicts", {})

    # ── shave_report.json ────────────────────────────────────────────────────
    shave = _safe_read_json(config.PL_SHAVE_REPORT)
    if shave:
        report = shave.get("report", {})
        result["shave_suspects"] = [
            adv_id
            for adv_id, info in report.items()
            if info.get("verdict") == "shave_suspected"
        ]

    logger.info(
        "[Tracking] PreLend: clicks=%d, CR=%.3f, bots=%.1f%%, geo=%s, shave_suspects=%d",
        result["total_clicks"],
        result["cr"] or 0,
        result["bot_pct"] or 0,
        result["top_geo"] or "?",
        len(result["shave_suspects"]),
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
            "top_uploads":    sp["raw_uploads"][:5],
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
            "analyst_verdicts":pl["analyst_verdicts"],
        },
    )

    return {"shorts_project": sp, "prelend": pl}
