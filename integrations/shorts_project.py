"""
integrations/shorts_project.py — Доступ к ShortsProject.

Предоставляет функции для чтения структуры аккаунтов SP и мониторинга здоровья.
Никогда не импортирует код из ShortsProject напрямую — только читает файлы.
Это предотвращает зависимость Orchestrator от внутренних изменений SP.

Экспортирует:
    get_all_accounts()              → список аккаунтов с конфигами
    get_account_config(name)        → конфиг конкретного аккаунта
    get_crash_loop_agents(window, n) → список агентов в краш-лупе
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import config

logger = logging.getLogger(__name__)


def get_all_accounts() -> List[Dict]:
    """
    Читает все аккаунты из SHORTS_PROJECT_DIR/accounts/.
    Повторяет логику pipeline/utils.get_all_accounts() из ShortsProject,
    но без импорта из SP — только файловый доступ.

    Возвращает список dict:
        name, dir (Path), platforms, config (dict из config.json)
    """
    accounts_root = config.SP_ACCOUNTS_DIR
    if not accounts_root.exists():
        logger.warning("[SP Integration] Директория аккаунтов не найдена: %s", accounts_root)
        return []

    accounts = []
    for acc_dir in sorted(accounts_root.iterdir()):
        if not acc_dir.is_dir():
            continue

        cfg_path = acc_dir / "config.json"
        if not cfg_path.exists():
            continue

        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[SP Integration] Не удалось прочитать %s: %s", cfg_path, exc)
            continue

        accounts.append({
            "name":      acc_dir.name,
            "dir":       acc_dir,
            "platforms": cfg.get("platforms", []),
            "config":    cfg,
        })

    logger.debug("[SP Integration] Загружено аккаунтов: %d", len(accounts))
    return accounts


def get_account_config(account_name: str) -> Optional[Dict]:
    """Возвращает конфиг конкретного аккаунта или None."""
    cfg_path = config.SP_ACCOUNTS_DIR / account_name / "config.json"
    if not cfg_path.exists():
        return None
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def get_crash_loop_agents(
    window_minutes: int = 60,
    min_restart_requests: int = 3,
) -> List[str]:
    """
    Читает agent_memory.json и возвращает имена агентов в краш-лупе.

    Краш-луп = SENTINEL запрашивал рестарт одного и того же агента
    min_restart_requests+ раз за последние window_minutes минут.

    SENTINEL пишет события: {"event": "restart_requested", "data": {"agent": "EDITOR"}, ...}
    в список events в agent_memory.json.

    Используется code_evolver.py для принятия решения об откате патча.
    """
    mem_path = config.SP_AGENT_MEMORY
    if not mem_path.exists():
        return []

    try:
        data = json.loads(mem_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("[SP Integration] Не удалось прочитать agent_memory: %s", exc)
        return []

    events = data.get("events", [])
    if not events:
        return []

    cutoff = (datetime.now() - timedelta(minutes=window_minutes)).isoformat()

    counts: Dict[str, int] = {}
    for event in events:
        if event.get("event") != "restart_requested":
            continue
        ts = event.get("ts", "")
        if ts and ts < cutoff:
            continue
        agent = (event.get("data") or {}).get("agent", "")
        if agent:
            counts[agent] = counts.get(agent, 0) + 1

    crash_agents = [name for name, cnt in counts.items() if cnt >= min_restart_requests]
    if crash_agents:
        logger.warning("[SP Integration] Краш-луп агентов: %s", crash_agents)
    return crash_agents
