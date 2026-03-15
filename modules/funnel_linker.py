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
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import config
from db.connection import get_db

logger = logging.getLogger(__name__)


# ── Публичный API ──────────────────────────────────────────────────────────


def link_funnel() -> int:
    """
    Синхронизирует SP analytics.json с PreLend clicks.db → funnel_events.
    Возвращает количество обработанных записей.
    """
    sp_data  = _load_sp_analytics()
    if not sp_data:
        logger.debug("[FunnelLinker] SP analytics.json пуст или не найден")
        return 0

    pl_conn = _open_prelend_db()
    if pl_conn is None:
        logger.warning("[FunnelLinker] PreLend clicks.db недоступен")
        return 0

    updated = 0
    try:
        with get_db() as orc_conn:
            for stem, entry in sp_data.items():
                rows = _build_funnel_rows(stem, entry, pl_conn)
                for row in rows:
                    _upsert_funnel_event(orc_conn, row)
                    updated += 1
    finally:
        pl_conn.close()

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


def _open_prelend_db() -> Optional[sqlite3.Connection]:
    """Открывает PreLend clicks.db только на чтение."""
    db_path = Path(config.PL_CLICKS_DB)
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        logger.warning("[FunnelLinker] Не удалось открыть PreLend DB: %s", exc)
        return None


def _build_funnel_rows(
    stem: str,
    entry: Dict,
    pl_conn: sqlite3.Connection,
) -> List[Dict]:
    """
    Для одного видео строит список строк воронки (по платформам).
    """
    rows: List[Dict] = []
    sub_id = f"sp_{stem}"

    # Клики по sub_id в PreLend (utm_content = sub_id)
    try:
        click_row = pl_conn.execute(
            "SELECT COUNT(*) as cnt FROM clicks WHERE utm_content = ? AND is_test = 0",
            (sub_id,),
        ).fetchone()
        total_clicks = click_row["cnt"] if click_row else 0
    except sqlite3.Error:
        total_clicks = 0

    # Конверсии (ищем в notes поле sub_id)
    try:
        conv_row = pl_conn.execute(
            "SELECT COUNT(*) as cnt, SUM(CASE WHEN notes LIKE ? THEN 0 ELSE 0 END) as payout"
            " FROM conversions WHERE notes LIKE ?",
            (f"%{sub_id}%", f"%{sub_id}%"),
        ).fetchone()
        total_convs = conv_row["cnt"] if conv_row else 0
    except sqlite3.Error:
        total_convs = 0

    # Выручка: разбираем payout из notes для конверсий с этим sub_id
    revenue = _calc_revenue_for_sub_id(pl_conn, sub_id)

    # Разбираем по платформам
    uploads: Dict = entry.get("uploads", {})
    if not uploads:
        # Нет загрузок — создаём одну строку без платформы
        rows.append({
            "sp_stem":        stem,
            "platform":       None,
            "video_url":      None,
            "prelend_sub_id": sub_id,
            "views":          0,
            "clicks":         total_clicks,
            "conversions":    total_convs,
            "revenue_rub":    revenue,
        })
        return rows

    for platform, upload in uploads.items():
        views = upload.get("views", 0) or 0
        url   = upload.get("url", "")

        rows.append({
            "sp_stem":        stem,
            "platform":       platform,
            "video_url":      url,
            "prelend_sub_id": sub_id,
            "views":          int(views),
            "clicks":         total_clicks,  # общие клики на sub_id
            "conversions":    total_convs,
            "revenue_rub":    revenue,
        })

    return rows


def _calc_revenue_for_sub_id(pl_conn: sqlite3.Connection, sub_id: str) -> float:
    """Считает суммарный payout из PreLend conversions.notes для данного sub_id."""
    total = 0.0
    try:
        rows = pl_conn.execute(
            "SELECT notes FROM conversions WHERE notes LIKE ?",
            (f"%{sub_id}%",),
        ).fetchall()
        for row in rows:
            notes = row["notes"] or ""
            # Парсим 'payout=5.00'
            for part in notes.split(";"):
                part = part.strip()
                if part.startswith("payout="):
                    try:
                        total += float(part.split("=", 1)[1])
                    except (ValueError, IndexError):
                        pass
    except sqlite3.Error:
        pass
    return total


def _upsert_funnel_event(conn: sqlite3.Connection, row: Dict) -> None:
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
