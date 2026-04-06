"""
modules/funnel_linker.py — Кросс-проектная аналитика (воронка видео → конверсии).

Связывает:
  ShortsProject analytics.json → видео → prelend_sub_id (sp_{stem})
  PreLend clicks.db            → клики и конверсии по sub_id

Результат материализуется в таблицу funnel_events (orchestrator.db).

Алгоритм:
  1. Читает SP analytics.json — для каждого видео берёт:
     - stem, platform, video_url, views, prelend_sub_id
  2. Для каждого prelend_sub_id ищет в PreLend clicks.db:
     - COUNT(clicks) WHERE utm_content = sub_id
     - COUNT(conversions) WHERE notes LIKE '%sub_id%' или через join
  3. Вставляет / обновляет funnel_events (ON CONFLICT REPLACE по sp_stem+platform)
  4. Возвращает список строк воронки для ContentHub Dashboard

Экспортирует:
    link_funnel() → int   количество обновлённых записей
    get_funnel_data(limit) → List[dict]  данные для ContentHub
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import config
from db.connection import get_db

logger = logging.getLogger(__name__)


# ── Публичный API ──────────────────────────────────────────────────────────


def link_funnel() -> int:
    """
    Синхронизирует SP analytics.json с PreLend clicks (через Internal API) → funnel_events.
    Возвращает количество обработанных записей.
    """
    sp_data = _load_sp_analytics()
    if not sp_data:
        logger.debug("[FunnelLinker] SP analytics.json пуст или не найден")
        return 0

    from integrations.prelend_client import get_client
    client = get_client()

    if not client.is_available():
        logger.warning("[FunnelLinker] PreLend API недоступен — воронка не обновлена")
        return 0

    funnel_data = client.get_funnel_data(period_hours=168)
    pl_clicks   = funnel_data.get("clicks", [])
    pl_conv_notes = funnel_data.get("conversion_notes", [])

    updated = 0
    with get_db() as orc_conn:
        for stem, entry in sp_data.items():
            rows = _build_funnel_rows_from_api(stem, entry, pl_clicks, pl_conv_notes)
            for row in rows:
                _upsert_funnel_event(orc_conn, row)
                updated += 1

    logger.info("[FunnelLinker] Обновлено записей в funnel_events: %d", updated)
    return updated


def get_funnel_data(limit: int = 100) -> List[Dict]:
    """
    Возвращает записи воронки из orchestrator.db для ContentHub.
    Сортировка: по revenue_rub desc.
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT sp_stem, platform, video_url, prelend_sub_id,
                   views, clicks, conversions, revenue_rub, linked_at
            FROM funnel_events
            ORDER BY revenue_rub DESC, views DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Внутренние функции ────────────────────────────────────────────────────


def _load_sp_analytics() -> Optional[Dict]:
    """Читает SP analytics.json."""
    path = Path(config.SP_ANALYTICS_FILE)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[FunnelLinker] Ошибка чтения SP analytics.json: %s", exc)
        return None


def _build_funnel_rows_from_api(
    stem: str,
    entry: Dict,
    pl_clicks: List[Dict],
    pl_conv_notes: List[Dict],
) -> List[Dict]:
    """
    Строит строки воронки для одного видео на основе данных из Internal API.

    pl_clicks — строки из GET /metrics/funnel: {utm_content, geo, status, cnt}
    pl_conv_notes — строки conversions.notes для расчёта revenue
    """
    sub_id = f"sp_{stem}"
    legacy_candidates = {sub_id}

    # Агрегируем клики по нашему sub_id
    total_clicks = 0
    total_convs = 0
    for r in pl_clicks:
        key = r.get("utm_content_key") or r.get("utm_content")
        if key in legacy_candidates:
            total_clicks += int(r.get("cnt") or 0)
            if r.get("status") == "converted":
                total_convs += int(r.get("cnt") or 0)

    # Выручка через notes
    revenue = _calc_revenue_from_notes(sub_id, pl_conv_notes)

    uploads: Dict = entry.get("uploads", {})
    if not uploads:
        return [{
            "sp_stem":        stem,
            "platform":       None,
            "video_url":      None,
            "prelend_sub_id": sub_id,
            "views":          0,
            "clicks":         total_clicks,
            "conversions":    total_convs,
            "revenue_rub":    revenue,
        }]

    rows = []
    for platform, upload in uploads.items():
        rows.append({
            "sp_stem":        stem,
            "platform":       platform,
            "video_url":      upload.get("url", ""),
            "prelend_sub_id": sub_id,
            "views":          int(upload.get("views") or 0),
            "clicks":         total_clicks,
            "conversions":    total_convs,
            "revenue_rub":    revenue,
        })
    return rows


def _calc_revenue_from_notes(sub_id: str, conv_notes: List[Dict]) -> float:
    """Считает суммарный payout из списка notes-строк для данного sub_id."""
    total = 0.0
    for row in conv_notes:
        notes = row.get("notes", "") or ""
        if sub_id not in notes:
            continue
        for part in notes.split(";"):
            part = part.strip()
            if part.startswith("payout="):
                try:
                    total += float(part.split("=", 1)[1]) * int(row.get("count", 1))
                except (ValueError, IndexError):
                    pass
    return total


def _upsert_funnel_event(conn, row: Dict) -> None:
    """Вставляет или обновляет запись в funnel_events."""
    conn.execute(
        """
        INSERT INTO funnel_events
            (sp_stem, platform, video_url, prelend_sub_id, views, clicks, conversions, revenue_rub)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO UPDATE SET
            video_url    = excluded.video_url,
            views        = excluded.views,
            clicks       = excluded.clicks,
            conversions  = excluded.conversions,
            revenue_rub  = excluded.revenue_rub,
            linked_at    = datetime('now')
        WHERE sp_stem = excluded.sp_stem AND platform IS excluded.platform
        """,
        (
            row["sp_stem"],
            row.get("platform"),
            row.get("video_url"),
            row["prelend_sub_id"],
            row["views"],
            row["clicks"],
            row["conversions"],
            row["revenue_rub"],
        ),
    )
