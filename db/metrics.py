"""
db/metrics.py — Сохранение и выборка снапшотов метрик.

Экспортирует:
    save_snapshot(...)          → id снапшота
    get_latest_snapshot(source) → последний снапшот из заданного источника
    get_metrics_trend(hours)    → тренд метрик за N часов (для LLM-анализа)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from db.connection import get_db

logger = logging.getLogger(__name__)


def save_snapshot(
    source: str,                    # 'ShortsProject' | 'PreLend'
    period_hours: int = 24,
    # ShortsProject поля
    sp_total_views:  Optional[int]  = None,
    sp_total_likes:  Optional[int]  = None,
    sp_avg_ctr:      Optional[float]= None,
    sp_top_platform: Optional[str]  = None,
    sp_ab_winner:    Optional[str]  = None,
    sp_ban_count:    Optional[int]  = None,
    # PreLend поля
    pl_total_clicks: Optional[int]  = None,
    pl_conversions:  Optional[int]  = None,
    pl_cr:           Optional[float]= None,
    pl_bot_pct:      Optional[float]= None,
    pl_top_geo:      Optional[str]  = None,
    # Сырые данные
    raw_summary:     Optional[Dict] = None,
) -> int:
    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO metrics_snapshots (
                source, period_hours,
                sp_total_views, sp_total_likes, sp_avg_ctr, sp_top_platform, sp_ab_winner, sp_ban_count,
                pl_total_clicks, pl_conversions, pl_cr, pl_bot_pct, pl_top_geo,
                raw_summary_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            source, period_hours,
            sp_total_views, sp_total_likes, sp_avg_ctr, sp_top_platform, sp_ab_winner, sp_ban_count,
            pl_total_clicks, pl_conversions, pl_cr, pl_bot_pct, pl_top_geo,
            json.dumps(raw_summary, ensure_ascii=False) if raw_summary else None,
        ))
        return cursor.lastrowid


def get_latest_snapshot(source: str) -> Optional[Dict]:
    """Возвращает последний снапшот из данного источника."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM metrics_snapshots WHERE source = ? ORDER BY snapshot_at DESC LIMIT 1",
            (source,)
        ).fetchone()
    return dict(row) if row else None


def get_metrics_trend(source: str, last_n_snapshots: int = 5) -> List[Dict]:
    """
    Возвращает последние N снапшотов для анализа тренда.
    LLM может видеть динамику: растут ли просмотры, CR и т.д.
    """
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM metrics_snapshots WHERE source = ?
               ORDER BY snapshot_at DESC LIMIT ?""",
            (source, last_n_snapshots)
        ).fetchall()
    return [dict(row) for row in rows]
