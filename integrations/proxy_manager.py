"""
integrations/proxy_manager.py — Клиент API mobileproxy.space.

Управляет мобильными прокси ShortsProject:
  - Мониторинг активных прокси и срока оплаты
  - Смена IP и оборудования
  - Покупка / продление прокси (только после подтверждения оператора)
  - Проверка баланса

Документация API: https://mobileproxy.space/user.html?api
Лимит: 3 запроса/сек (смена IP — без ограничений).

Экспортирует:
    get_balance()                → float руб. или None
    get_my_proxies(ids)          → list[dict]
    get_expiring_proxies(days)   → list[dict]
    rotate_ip(proxy)             → bool
    estimate_purchase(...)       → float руб. или None
    buy_proxy(...)               → list[int] купленных proxy_id
    renew_proxies(ids)           → list[int] продлённых proxy_id
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

import config

logger = logging.getLogger(__name__)

_API_BASE    = "https://mobileproxy.space/api.html"
_CHANGE_IP   = "https://changeip.mobileproxy.space/"
_TIMEOUT_SEC = 15


# ─────────────────────────────────────────────────────────────────────────────
# Внутренние хелперы
# ─────────────────────────────────────────────────────────────────────────────

def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {config.MOBILEPROXY_API_KEY}"}


def _get(command: str, **params: Any) -> Optional[Dict]:
    """GET-запрос к API. Возвращает JSON или None при ошибке."""
    if not config.MOBILEPROXY_API_KEY:
        logger.debug("[ProxyManager] API-ключ не задан (ORC_MOBILEPROXY_API_KEY)")
        return None
    try:
        resp = requests.get(
            _API_BASE,
            headers=_headers(),
            params={"command": command, **params},
            timeout=_TIMEOUT_SEC,
        )
        if resp.status_code != 200:
            logger.warning("[ProxyManager] HTTP %d для команды %s", resp.status_code, command)
            return None
        data = resp.json()
        if str(data.get("status", "")).lower() != "ok":
            logger.warning("[ProxyManager] API error [%s]: %s", command, str(data)[:120])
            return None
        return data
    except Exception as exc:
        logger.warning("[ProxyManager] Запрос %s: %s", command, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Баланс
# ─────────────────────────────────────────────────────────────────────────────

def get_balance() -> Optional[float]:
    """Возвращает баланс аккаунта в рублях или None."""
    data = _get("get_balance")
    if data is None:
        return None
    try:
        return float(data["balance"])
    except (KeyError, TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Список прокси
# ─────────────────────────────────────────────────────────────────────────────

def get_my_proxies(proxy_ids: Optional[List[int]] = None) -> List[Dict]:
    """
    Возвращает список активных прокси.
    proxy_ids=None → все прокси аккаунта.
    """
    params: Dict[str, Any] = {}
    if proxy_ids:
        params["proxy_id"] = ",".join(str(x) for x in proxy_ids)
    data = _get("get_my_proxy", **params)
    if not data:
        return []
    # API возвращает dict {proxy_id: {...}} или list
    raw = data.get("proxy_id", data)
    if isinstance(raw, dict):
        return list(raw.values())
    if isinstance(raw, list):
        return raw
    return []


def get_expiring_proxies(within_days: int = 3) -> List[Dict]:
    """Прокси с истекающим сроком оплаты (≤ N дней)."""
    proxies = get_my_proxies()
    cutoff  = datetime.now() + timedelta(days=within_days)
    result  = []
    for p in proxies:
        exp_str = p.get("proxy_exp", "")
        if not exp_str:
            continue
        try:
            # "2026-03-15 23:59:59" или ISO
            exp_dt = datetime.fromisoformat(exp_str.replace(" ", "T").split(".")[0])
            if exp_dt <= cutoff:
                result.append(p)
        except ValueError:
            pass
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Смена IP
# ─────────────────────────────────────────────────────────────────────────────

def rotate_ip(proxy: Dict) -> bool:
    """
    Меняет IP прокси. Использует ссылку из proxy_change_ip_url.
    proxy — dict из get_my_proxies().
    """
    change_url = proxy.get("proxy_change_ip_url", "")
    if not change_url:
        logger.warning("[ProxyManager] rotate_ip: нет proxy_change_ip_url в proxy %s",
                       proxy.get("proxy_id"))
        return False
    try:
        resp = requests.get(
            change_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"},
            params={"format": "json"},
            timeout=_TIMEOUT_SEC,
        )
        data = resp.json()
        ok = data.get("code") == 200 or data.get("status") == "ok"
        if ok:
            logger.info("[ProxyManager] IP сменён для прокси %s → %s",
                        proxy.get("proxy_id"), data.get("new_ip", "?"))
        return ok
    except Exception as exc:
        logger.warning("[ProxyManager] rotate_ip: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Покупка и продление
# ─────────────────────────────────────────────────────────────────────────────

def estimate_purchase(
    geo_id: int,
    operator: Optional[str] = None,
    num: int = 1,
    period: int = 30,
) -> Optional[float]:
    """
    Возвращает расчётную стоимость покупки без её совершения (amount_only=true).
    """
    params: Dict[str, Any] = {
        "geoid":       geo_id,
        "num":         num,
        "period":      period,
        "amount_only": "true",
    }
    if operator:
        params["operator"] = operator
    data = _get("buyproxy", **params)
    if not data:
        return None
    try:
        return float(data.get("amount", 0))
    except (TypeError, ValueError):
        return None


def buy_proxy(
    geo_id: int,
    operator: Optional[str] = None,
    num: int = 1,
    period: int = 30,
    auto_renewal: int = 1,
) -> List[int]:
    """
    Покупает новые прокси. Вызывать ТОЛЬКО после подтверждения оператором.
    Возвращает список ID купленных прокси или [].
    """
    params: Dict[str, Any] = {
        "geoid":        geo_id,
        "num":          num,
        "period":       period,
        "auto_renewal": auto_renewal,
    }
    if operator:
        params["operator"] = operator
    data = _get("buyproxy", **params)
    if not data:
        logger.error("[ProxyManager] buy_proxy: API вернула ошибку")
        return []
    ids    = data.get("proxy_id", [])
    amount = data.get("amount", 0)
    logger.info("[ProxyManager] Куплено %s прокси (сумма %s руб.)", ids, amount)
    return ids if isinstance(ids, list) else ([ids] if ids else [])


def renew_proxies(proxy_ids: List[int], period: int = 30) -> List[int]:
    """
    Продлевает конкретные прокси. Вызывать ТОЛЬКО после подтверждения оператором.
    Возвращает список ID продлённых прокси или [].
    """
    if not proxy_ids:
        return []
    params: Dict[str, Any] = {
        "proxy_id": ",".join(str(p) for p in proxy_ids),
        "period":   period,
    }
    data = _get("buyproxy", **params)
    if not data:
        logger.error("[ProxyManager] renew_proxies: API вернула ошибку")
        return []
    ids = data.get("proxy_id", proxy_ids)
    logger.info("[ProxyManager] Продлено %s прокси", ids)
    return ids if isinstance(ids, list) else proxy_ids
