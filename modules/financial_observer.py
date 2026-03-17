"""
modules/financial_observer.py — FinancialObserver.

Автоматический сбор финансовых данных из всех проектов:
  - Revenue (доходы):
      * PreLend конверсии с payout из clicks.db
      * ShortsProject монетизация из analytics.json
  - Expenses (расходы):
      * Прокси из proxy_events (orchestrator.db)
      * Ручной ввод через ContentHub UI или /add_expense Telegram

Результат сохраняется в financial_records (orchestrator.db).

Экспорт для evolution.py:
    get_financial_context() → dict с net_roi, P&L за 7/30 дней
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import config
from db.finances import add_record, get_summary, record_exists

logger = logging.getLogger(__name__)


# ── Константы ─────────────────────────────────────────────────────────────────

_SP_ANALYTICS = config.SP_ANALYTICS_FILE  # ShortsProject analytics.json
_ORC_DB       = config.DB_PATH            # orchestrator.db


# ─────────────────────────────────────────────────────────────────────────────
# Публичные функции
# ─────────────────────────────────────────────────────────────────────────────

def collect_all() -> Dict[str, int]:
    """
    Запускает сбор всех источников дохода и расходов.
    Возвращает словарь с количеством новых записей по каждому источнику.
    """
    results = {
        "prelend_revenue": 0,
        "sp_revenue": 0,
        "proxy_expenses": 0,
    }

    results["prelend_revenue"] = _collect_prelend_revenue()
    results["sp_revenue"]      = _collect_sp_revenue()
    results["proxy_expenses"]  = _collect_proxy_expenses()

    total = sum(results.values())
    logger.info("[FinancialObserver] Сбор завершён: %d новых записей (%s)", total, results)
    return results


def get_financial_context(days: int = 30) -> Dict[str, Any]:
    """
    Возвращает финансовый контекст для инжекции в LLM-промпт evolution.py.
    Включает net_roi, выручку, расходы за последние `days` дней.
    """
    summary = get_summary(days=days)
    summary_7d = get_summary(days=7)

    return {
        "net_roi_rub":      summary["net_rub"],
        "revenue_rub":      summary["revenue_rub"],
        "expense_rub":      summary["expense_rub"],
        "roi_pct":          summary["roi_pct"],
        "net_roi_7d_rub":   summary_7d["net_rub"],
        "roi_7d_pct":       summary_7d["roi_pct"],
        "by_source":        summary["by_source"],
        "period_days":      days,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Сбор доходов — PreLend
# ─────────────────────────────────────────────────────────────────────────────

def _collect_prelend_revenue() -> int:
    """
    Получает конверсии с payout из PreLend через Internal API.
    Дедупликация по external_id = 'pl_conv_{conv_id}'.
    """
    from integrations.prelend_client import get_client
    client = get_client()

    if not client.is_available():
        logger.debug("[FinancialObserver] PreLend API недоступен — пропуск revenue")
        return 0

    data = client.get_financial_metrics(period_hours=35 * 24)
    rows = data.get("conversions", [])
    if not rows:
        return 0

    added = 0
    for row in rows:
        ext_id = f"pl_conv_{row.get('id', '')}"
        if not row.get("id") or record_exists(ext_id):
            continue

        payout = _parse_payout_from_notes(row.get("notes", ""))
        if payout <= 0:
            continue

        add_record(
            category       = "revenue",
            source         = "affiliate",
            amount_rub     = payout,
            description    = f"PreLend конверсия: adv={row.get('advertiser_id')} date={row.get('date')}",
            period_start   = row.get("date", ""),
            period_end     = row.get("date", ""),
            external_id    = ext_id,
            auto_collected = True,
        )
        added += 1

    return added


def _parse_payout_from_notes(notes: str) -> float:
    """Парсит 'payout=5.00' из строки notes."""
    if not notes:
        return 0.0
    for part in notes.split(";"):
        part = part.strip()
        if part.startswith("payout="):
            try:
                return float(part.split("=", 1)[1])
            except (ValueError, IndexError):
                pass
    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Сбор доходов — ShortsProject монетизация
# ─────────────────────────────────────────────────────────────────────────────

def _collect_sp_revenue() -> int:
    """
    Читает analytics.json ShortsProject.
    Ищет записи с полем 'monetization_rub' (если добавлено в uploader).
    """
    analytics_path = Path(_SP_ANALYTICS)
    if not analytics_path.exists():
        logger.debug("[FinancialObserver] SP analytics.json не найден: %s", analytics_path)
        return 0

    try:
        with open(analytics_path, encoding="utf-8") as f:
            analytics: List[Dict] = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[FinancialObserver] Ошибка чтения SP analytics.json: %s", exc)
        return 0

    added = 0
    for entry in analytics:
        # Ожидаем поля: stem, upload_date, monetization_rub, platform
        mon = entry.get("monetization_rub", 0)
        if not mon or mon <= 0:
            continue

        stem    = entry.get("stem", "")
        plat    = entry.get("platform", "unknown")
        upl_date= entry.get("upload_date", date.today().isoformat())
        ext_id  = f"sp_mon_{stem}_{plat}"

        if record_exists(ext_id):
            continue

        add_record(
            category      = "revenue",
            source        = "monetization",
            amount_rub    = float(mon),
            description   = f"SP монетизация: {stem} ({plat}) {upl_date}",
            period_start  = upl_date,
            period_end    = upl_date,
            external_id   = ext_id,
            auto_collected= True,
        )
        added += 1

    return added


# ─────────────────────────────────────────────────────────────────────────────
# Сбор расходов — прокси из orchestrator.db
# ─────────────────────────────────────────────────────────────────────────────

def _collect_proxy_expenses() -> int:
    """
    Читает proxy_events (orchestrator.db) со статусом 'confirmed' и cost > 0.
    """
    added = 0
    try:
        conn = sqlite3.connect(str(_ORC_DB), timeout=5.0)
        conn.row_factory = sqlite3.Row
        since = (date.today() - timedelta(days=35)).isoformat()
        rows = conn.execute(
            """
            SELECT id, created_at, event_type, geo, operator, cost, reason
            FROM proxy_events
            WHERE status = 'confirmed'
              AND cost > 0
              AND date(created_at) >= ?
            ORDER BY created_at ASC
            """,
            (since,),
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("[FinancialObserver] Ошибка чтения proxy_events: %s", exc)
        return 0

    for row in rows:
        ext_id = f"proxy_evt_{row['id']}"
        if record_exists(ext_id):
            continue

        add_record(
            category      = "expense",
            source        = "proxies",
            amount_rub    = float(row["cost"]),
            description   = f"Прокси {row['event_type']}: geo={row['geo']} op={row['operator']} — {row['reason']}",
            period_start  = row["created_at"][:10],
            period_end    = row["created_at"][:10],
            external_id   = ext_id,
            auto_collected= True,
        )
        added += 1

    return added
