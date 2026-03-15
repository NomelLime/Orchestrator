"""
db/finances.py — CRUD для таблицы financial_records.

Используется FinancialObserver (modules/financial_observer.py) и ContentHub API.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from db.connection import get_db

logger = logging.getLogger(__name__)


# ── Запись ────────────────────────────────────────────────────────────────────

def add_record(
    category: str,       # 'expense' | 'revenue'
    source: str,         # 'proxies'|'accounts'|'apis'|'monetization'|'affiliate'|'manual'
    amount_rub: float,
    description: str = "",
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    external_id: Optional[str] = None,
    auto_collected: bool = True,
) -> int:
    """Добавляет запись, возвращает id."""
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO financial_records
                (category, source, amount_rub, description,
                 period_start, period_end, external_id, auto_collected)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                category, source, amount_rub, description,
                period_start, period_end, external_id,
                1 if auto_collected else 0,
            ),
        )
        return cur.lastrowid


def record_exists(external_id: str) -> bool:
    """Проверяет дубликат по external_id."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM financial_records WHERE external_id = ? LIMIT 1",
            (external_id,),
        ).fetchone()
        return row is not None


# ── Чтение ────────────────────────────────────────────────────────────────────

def get_summary(days: int = 30) -> Dict[str, Any]:
    """
    Возвращает агрегированные финансовые данные за последние `days` дней.
    {
      "revenue_rub": float,
      "expense_rub": float,
      "net_rub": float,
      "roi_pct": float,
      "by_source": {"proxies": {"expense": 0, "revenue": 0}, ...},
      "by_day": [{"date": "YYYY-MM-DD", "revenue": 0, "expense": 0}, ...]
    }
    """
    since = (date.today() - timedelta(days=days)).isoformat()

    with get_db() as conn:
        # Суммы по категориям
        totals_rows = conn.execute(
            """
            SELECT category, SUM(amount_rub) as total
            FROM financial_records
            WHERE date(recorded_at) >= ?
            GROUP BY category
            """,
            (since,),
        ).fetchall()
        totals = {r["category"]: (r["total"] or 0) for r in totals_rows}
        revenue = totals.get("revenue", 0.0)
        expense = totals.get("expense", 0.0)
        net     = revenue - expense
        roi_pct = ((net / expense) * 100) if expense > 0 else 0.0

        # По источникам
        src_rows = conn.execute(
            """
            SELECT source, category, SUM(amount_rub) as total
            FROM financial_records
            WHERE date(recorded_at) >= ?
            GROUP BY source, category
            """,
            (since,),
        ).fetchall()
        by_source: Dict[str, Dict] = {}
        for r in src_rows:
            s = r["source"]
            if s not in by_source:
                by_source[s] = {"expense": 0.0, "revenue": 0.0}
            by_source[s][r["category"]] = r["total"] or 0.0

        # По дням
        day_rows = conn.execute(
            """
            SELECT date(recorded_at) as d, category, SUM(amount_rub) as total
            FROM financial_records
            WHERE date(recorded_at) >= ?
            GROUP BY d, category
            ORDER BY d ASC
            """,
            (since,),
        ).fetchall()
        by_day_map: Dict[str, Dict] = {}
        for r in day_rows:
            d = r["d"]
            if d not in by_day_map:
                by_day_map[d] = {"date": d, "revenue": 0.0, "expense": 0.0}
            by_day_map[d][r["category"]] = r["total"] or 0.0
        by_day = sorted(by_day_map.values(), key=lambda x: x["date"])

    return {
        "revenue_rub": round(revenue, 2),
        "expense_rub": round(expense, 2),
        "net_rub":     round(net, 2),
        "roi_pct":     round(roi_pct, 2),
        "by_source":   by_source,
        "by_day":      by_day,
        "period_days": days,
    }


def get_recent_records(limit: int = 50) -> List[Dict]:
    """Возвращает последние `limit` записей."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, recorded_at, category, source, amount_rub,
                   description, period_start, period_end, external_id, auto_collected
            FROM financial_records
            ORDER BY recorded_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
