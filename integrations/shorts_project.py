"""
integrations/shorts_project.py — Доступ к ShortsProject.

Предоставляет функции для чтения структуры аккаунтов SP.
Никогда не импортирует код из ShortsProject напрямую — только читает файлы.
Это предотвращает зависимость Orchestrator от внутренних изменений SP.

Экспортирует:
    get_all_accounts()          → список аккаунтов с конфигами
    get_account_config(name)    → конфиг конкретного аккаунта
"""

from __future__ import annotations

import json
import logging
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
