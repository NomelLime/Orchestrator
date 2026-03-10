"""
integrations/prelend.py — Доступ к PreLend.

Читает настройки и данные PreLend без импорта PHP или Python-кода SP.

Экспортирует:
    get_settings()          → dict из settings.json
    get_advertisers()       → список рекламодателей
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

import config

logger = logging.getLogger(__name__)


def get_settings() -> Dict:
    """Читает config/settings.json PreLend."""
    try:
        return json.loads(config.PL_SETTINGS.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[PreLend Integration] Не удалось прочитать settings.json: %s", exc)
        return {}


def get_advertisers() -> List[Dict]:
    """Читает config/advertisers.json PreLend."""
    try:
        return json.loads(config.PL_ADVERTISERS.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[PreLend Integration] Не удалось прочитать advertisers.json: %s", exc)
        return []
