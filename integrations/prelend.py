"""
integrations/prelend.py — Обратная совместимость.

Делегирует все вызовы в prelend_client.py (HTTP → PreLend Internal API).
Прямой доступ к файлам PreLend через файловую систему убран:
PreLend теперь на VPS, доступен только через Internal API.

Экспортирует (те же имена, что были раньше):
    get_settings()    → dict
    get_advertisers() → list
"""
from __future__ import annotations

from typing import Dict, List

from .prelend_client import get_client


def get_settings() -> Dict:
    """Читает config/settings.json PreLend через Internal API."""
    return get_client().get_settings()


def get_advertisers() -> List[Dict]:
    """Читает config/advertisers.json PreLend через Internal API."""
    return get_client().get_advertisers()
