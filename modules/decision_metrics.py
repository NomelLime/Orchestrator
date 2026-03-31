"""
decision_metrics.py — производные KPI для цикла Orchestrator.

Считает:
  - качество решений (plan/patch success rates, rollback rates),
  - свежесть и доступность источников метрик,
  - краткосрочные дельты PreLend.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from db.connection import get_db


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    candidates = [
        s,
        s.replace("Z", "+00:00"),
        s.replace(" UTC", ""),
    ]
    for raw in candidates:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                dt = datetime.strptime(raw, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        try:
            dt2 = datetime.fromisoformat(raw)
            if dt2.tzinfo is None:
                dt2 = dt2.replace(tzinfo=timezone.utc)
            return dt2.astimezone(timezone.utc)
        except ValueError:
            pass
    return None


def _ratio(num: int, den: int) -> Optional[float]:
    if den <= 0:
        return None
    return round(num / den, 4)


def _latest_snapshot_info(source: str) -> tuple[Optional[datetime], Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT snapshot_at, raw_summary_json
            FROM metrics_snapshots
            WHERE source = ?
            ORDER BY snapshot_at DESC
            LIMIT 1
            """,
            (source,),
        ).fetchone()
    if not row:
        return None, {}
    raw = {}
    try:
        if row["raw_summary_json"]:
            raw = json.loads(row["raw_summary_json"])
    except Exception:
        raw = {}
    return _parse_dt(row["snapshot_at"]), raw


def _prelend_delta_24h() -> Dict[str, Optional[float]]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT pl_cr, pl_bot_pct
            FROM metrics_snapshots
            WHERE source = 'PreLend'
            ORDER BY snapshot_at DESC
            LIMIT 2
            """
        ).fetchall()
    if len(rows) < 2:
        return {"cr_delta_24h": None, "bot_pct_delta_24h": None}
    cur, prev = rows[0], rows[1]
    cur_cr = cur["pl_cr"] if cur["pl_cr"] is not None else None
    prev_cr = prev["pl_cr"] if prev["pl_cr"] is not None else None
    cur_bot = cur["pl_bot_pct"] if cur["pl_bot_pct"] is not None else None
    prev_bot = prev["pl_bot_pct"] if prev["pl_bot_pct"] is not None else None
    return {
        "cr_delta_24h": (round(cur_cr - prev_cr, 4) if cur_cr is not None and prev_cr is not None else None),
        "bot_pct_delta_24h": (round(cur_bot - prev_bot, 4) if cur_bot is not None and prev_bot is not None else None),
    }


def collect_decision_kpis() -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    cutoff_30d = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff_24h = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        plans_total = conn.execute(
            "SELECT COUNT(*) AS c FROM evolution_plans WHERE created_at >= ?",
            (cutoff_30d,),
        ).fetchone()["c"]
        plans_applied = conn.execute(
            "SELECT COUNT(*) AS c FROM evolution_plans WHERE created_at >= ? AND status = 'applied'",
            (cutoff_30d,),
        ).fetchone()["c"]

        plan_quality_total = conn.execute(
            "SELECT COUNT(*) AS c FROM plan_quality_scores WHERE evaluated_at >= ?",
            (cutoff_30d,),
        ).fetchone()["c"]
        plan_quality_good = conn.execute(
            "SELECT COUNT(*) AS c FROM plan_quality_scores WHERE evaluated_at >= ? AND overall_score > 0",
            (cutoff_30d,),
        ).fetchone()["c"]

        changes_total = conn.execute(
            "SELECT COUNT(*) AS c FROM applied_changes WHERE applied_at >= ?",
            (cutoff_30d,),
        ).fetchone()["c"]
        changes_rolled_back = conn.execute(
            "SELECT COUNT(*) AS c FROM applied_changes WHERE applied_at >= ? AND rolled_back = 1",
            (cutoff_30d,),
        ).fetchone()["c"]

        patches_total = conn.execute(
            "SELECT COUNT(*) AS c FROM pending_patches WHERE created_at >= ?",
            (cutoff_30d,),
        ).fetchone()["c"]
        patches_applied = conn.execute(
            "SELECT COUNT(*) AS c FROM pending_patches WHERE created_at >= ? AND status = 'applied'",
            (cutoff_30d,),
        ).fetchone()["c"]
        patches_failed = conn.execute(
            "SELECT COUNT(*) AS c FROM pending_patches WHERE created_at >= ? AND status = 'failed'",
            (cutoff_30d,),
        ).fetchone()["c"]

        pl_total_24h = conn.execute(
            "SELECT COUNT(*) AS c FROM metrics_snapshots WHERE source='PreLend' AND snapshot_at >= ?",
            (cutoff_24h,),
        ).fetchone()["c"]
        pl_reachable_24h = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM metrics_snapshots
            WHERE source='PreLend' AND snapshot_at >= ?
              AND (raw_summary_json NOT LIKE '%"_unreachable": true%'
                   AND raw_summary_json NOT LIKE '%"_unreachable":true%')
            """,
            (cutoff_24h,),
        ).fetchone()["c"]

        sp_total_24h = conn.execute(
            "SELECT COUNT(*) AS c FROM metrics_snapshots WHERE source='ShortsProject' AND snapshot_at >= ?",
            (cutoff_24h,),
        ).fetchone()["c"]

    sp_latest_ts, _ = _latest_snapshot_info("ShortsProject")
    pl_latest_ts, pl_raw = _latest_snapshot_info("PreLend")
    deltas = _prelend_delta_24h()

    return {
        "plan_apply_rate_30d": _ratio(plans_applied, plans_total),
        "plan_success_24h_rate_30d": _ratio(plan_quality_good, plan_quality_total),
        "rollback_rate_30d": _ratio(changes_rolled_back, changes_total),
        "patch_apply_success_rate_30d": _ratio(patches_applied, patches_total),
        "patch_test_fail_rate_30d": _ratio(patches_failed, patches_total),
        "patch_revert_rate_30d": _ratio(changes_rolled_back, max(1, patches_applied)),
        "metrics_freshness_sec_sp": int((now - sp_latest_ts).total_seconds()) if sp_latest_ts else None,
        "metrics_freshness_sec_pl": int((now - pl_latest_ts).total_seconds()) if pl_latest_ts else None,
        "source_availability_ratio_pl_24h": _ratio(pl_reachable_24h, pl_total_24h),
        "source_availability_ratio_sp_24h": _ratio(sp_total_24h, sp_total_24h) if sp_total_24h > 0 else None,
        "pl_traffic_alive": pl_raw.get("traffic_alive"),
        "pl_last_click_ago_sec": pl_raw.get("last_click_ago_sec"),
        **deltas,
    }

