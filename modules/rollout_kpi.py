from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import config

_KPI_FILE = Path(config.BASE_DIR) / "data" / "rollout_kpi.json"


def record_rollout_kpi(metrics_data: Dict[str, Any]) -> Dict[str, Any]:
    sp = metrics_data.get("shorts_project") or {}
    pl = metrics_data.get("prelend") or {}
    kpi = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "retention_proxy": round(float(sp.get("avg_ctr") or 0.0), 6),
        "views": int(sp.get("total_views") or 0),
        "clicks": int(pl.get("total_clicks") or 0),
        "conversions": int(pl.get("conversions") or 0),
        "cr": float(pl.get("cr") or 0.0),
        "bot_pct": float(pl.get("bot_pct") or 0.0),
    }

    payload = {"history": []}
    if _KPI_FILE.exists():
        try:
            payload = json.loads(_KPI_FILE.read_text(encoding="utf-8"))
        except Exception:
            payload = {"history": []}
    history = payload.get("history")
    if not isinstance(history, list):
        history = []
    history.append(kpi)
    payload["history"] = history[-500:]
    _KPI_FILE.parent.mkdir(parents=True, exist_ok=True)
    _KPI_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return kpi
