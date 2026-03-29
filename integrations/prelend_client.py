"""
integrations/prelend_client.py — HTTP-клиент к PreLend Internal API.

Заменяет прямой доступ к файлам PreLend через файловую систему.
Используется и Orchestrator, и ContentHub (через sys.path или копию).

Конфигурация через переменные окружения:
    PL_INTERNAL_API_URL  — URL API (default: http://localhost:9090)
    PL_INTERNAL_API_KEY  — ключ (default: пусто = dev-режим)
    PL_INTERNAL_TIMEOUT  — таймаут запроса в сек (default: 10)

Экспортирует:
    PreLendClient               — класс клиента
    get_client() → PreLendClient  — singleton
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Union

import requests
from requests import Response

logger = logging.getLogger(__name__)

_PL_API_URL = os.getenv("PL_INTERNAL_API_URL", "http://localhost:9090")
_PL_API_KEY = os.getenv("PL_INTERNAL_API_KEY", "")
_TIMEOUT    = int(os.getenv("PL_INTERNAL_TIMEOUT", "10"))


class PreLendClient:
    """HTTP-клиент к PreLend Internal API."""

    def __init__(
        self,
        base_url: str = _PL_API_URL,
        api_key:  str = _PL_API_KEY,
        timeout:  int = _TIMEOUT,
    ):
        self._base    = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._last_put_error: Optional[str] = None
        if api_key:
            self._session.headers["X-API-Key"] = api_key

    # ── Health ─────────────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Проверяет доступность Internal API (GET /health)."""
        try:
            r = self._session.get(f"{self._base}/health", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def get_health(self) -> Optional[Dict]:
        """Возвращает расширенные данные /health или None при недоступности."""
        try:
            r = self._session.get(f"{self._base}/health", timeout=5)
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    @property
    def base_url(self) -> str:
        """URL Internal API (для логирования)."""
        return self._base

    @property
    def last_put_error(self) -> Optional[str]:
        """Текст последней ошибки PUT /config/* (если write_* вернул False)."""
        return self._last_put_error

    @staticmethod
    def _format_put_failure(r: Response) -> str:
        code = r.status_code
        try:
            data = r.json()
            d = data.get("detail")
            if isinstance(d, str):
                msg = d
            elif isinstance(d, list):
                parts = []
                for item in d[:8]:
                    if isinstance(item, dict):
                        parts.append(str(item.get("msg", item)))
                    else:
                        parts.append(str(item))
                msg = "; ".join(parts) if parts else str(d)[:400]
            else:
                msg = str(data)[:500]
        except Exception:
            msg = (r.text or "").strip()[:500]
        return f"HTTP {code}: {msg}"

    # ── Метрики ────────────────────────────────────────────────────────────────

    def get_metrics(self, period_hours: int = 24) -> Dict[str, Any]:
        """
        Агрегированные метрики PreLend.
        Эквивалент collect_prelend_snapshot() из tracking.py.
        """
        return self._get("/metrics", params={"period_hours": period_hours})

    def get_financial_metrics(self, period_hours: int = 24) -> Dict[str, Any]:
        """Конверсии с payout для FinancialObserver."""
        return self._get("/metrics/financial", params={"period_hours": period_hours})

    def get_funnel_data(self, period_hours: int = 168) -> Dict[str, Any]:
        """Данные для cross-project воронки (SP stem → PL clicks)."""
        return self._get("/metrics/funnel", params={"period_hours": period_hours})

    # ── Конфиги ────────────────────────────────────────────────────────────────

    def get_config(self, name: str) -> Union[Dict, List]:
        """Читает конфиг: settings | advertisers | geo_data | splits."""
        return self._get(f"/config/{name}")

    def get_settings(self) -> Dict:
        return self.get_config("settings")

    def get_advertisers(self) -> List[Dict]:
        data = self.get_config("advertisers")
        return data if isinstance(data, list) else []

    def get_geo_data(self) -> Dict:
        return self.get_config("geo_data")

    def get_splits(self) -> List:
        data = self.get_config("splits")
        return data if isinstance(data, list) else []

    def get_templates(self) -> Dict[str, List[str]]:
        data = self._get("/templates")
        if not isinstance(data, dict):
            return {"offers": [], "cloaked": []}
        offers = data.get("offers", [])
        cloaked = data.get("cloaked", [])
        return {
            "offers": offers if isinstance(offers, list) else [],
            "cloaked": cloaked if isinstance(cloaked, list) else [],
        }

    def write_config(
        self, name: str, data: Any, source: str = "orchestrator"
    ) -> bool:
        """Атомарная запись конфига + git commit на VPS (PUT /config/{name})."""
        return self._put(
            f"/config/{name}",
            json_body=data,
            params={"source": source},
        )

    def write_settings(self, data: Dict, source: str = "orchestrator") -> bool:
        return self.write_config("settings", data, source)

    def write_advertisers(
        self, data: List[Dict], source: str = "orchestrator"
    ) -> bool:
        return self.write_config("advertisers", data, source)

    def write_geo_data(self, data: Dict, source: str = "orchestrator") -> bool:
        return self.write_config("geo_data", data, source)

    def write_splits(self, data: List, source: str = "orchestrator") -> bool:
        return self.write_config("splits", data, source)

    # ── Агенты ─────────────────────────────────────────────────────────────────

    def get_agents(self) -> List[Dict]:
        """Статусы агентов PreLend."""
        data = self._get("/agents")
        return data if isinstance(data, list) else []

    def stop_agent(self, name: str) -> bool:
        return self._post(f"/agents/{name}/stop")

    def start_agent(self, name: str) -> bool:
        return self._post(f"/agents/{name}/start")

    # ── Внутренние методы ──────────────────────────────────────────────────────

    def _get(self, path: str, params: Dict = None) -> Any:
        try:
            r = self._session.get(
                f"{self._base}{path}",
                params=params,
                timeout=self._timeout,
            )
            r.raise_for_status()
            return r.json()
        except requests.ConnectionError:
            logger.warning(
                "[PreLendClient] Нет связи с PreLend API (%s%s). "
                "Проверьте SSH tunnel / WireGuard.",
                self._base, path,
            )
            return {}
        except requests.HTTPError as exc:
            logger.warning("[PreLendClient] HTTP %s при GET %s: %s", exc.response.status_code, path, exc)
            return {}
        except Exception as exc:
            logger.warning("[PreLendClient] Ошибка GET %s: %s", path, exc)
            return {}

    def _put(self, path: str, json_body: Any = None, params: Dict = None) -> bool:
        self._last_put_error = None
        try:
            r = self._session.put(
                f"{self._base}{path}",
                json=json_body,
                params=params,
                timeout=self._timeout,
            )
            if r.ok:
                return True

            self._last_put_error = self._format_put_failure(r)

            # Backward compatibility for old Internal API versions:
            # some deployments expect payload wrapped as {"body": ...}.
            if r.status_code == 422 and json_body is not None:
                try:
                    data = r.json()
                except Exception:
                    data = {}
                detail = data.get("detail", "")
                need_wrapped_body = False
                if isinstance(detail, list):
                    for item in detail:
                        if not isinstance(item, dict):
                            continue
                        loc = item.get("loc", [])
                        msg = item.get("msg", "")
                        if isinstance(loc, list) and loc[:2] == ["body", "body"] and msg == "Field required":
                            need_wrapped_body = True
                            break
                else:
                    detail_text = str(detail)
                    if "body" in detail_text and "Field required" in detail_text:
                        need_wrapped_body = True

                if need_wrapped_body:
                    r2 = self._session.put(
                        f"{self._base}{path}",
                        json={"body": json_body},
                        params=params,
                        timeout=self._timeout,
                    )
                    if r2.ok:
                        self._last_put_error = None
                        logger.info("[PreLendClient] PUT %s: fallback payload format applied", path)
                        return True
                    self._last_put_error = self._format_put_failure(r2)

            logger.error("[PreLendClient] PUT %s не удался: %s", path, self._last_put_error)
            return False
        except requests.ConnectionError:
            self._last_put_error = (
                "Нет соединения с PreLend Internal API (проверьте SSH-туннель :9090 / WireGuard)."
            )
            logger.error(
                "[PreLendClient] Нет связи с PreLend API при PUT %s. "
                "Изменение конфига НЕ применено.",
                path,
            )
            return False
        except Exception as exc:
            self._last_put_error = str(exc)
            logger.error("[PreLendClient] Ошибка PUT %s: %s", path, exc)
            return False

    def _post(self, path: str, json_body: Any = None) -> bool:
        try:
            r = self._session.post(
                f"{self._base}{path}",
                json=json_body,
                timeout=self._timeout,
            )
            r.raise_for_status()
            return True
        except Exception as exc:
            logger.error("[PreLendClient] Ошибка POST %s: %s", path, exc)
            return False


# ── Singleton ──────────────────────────────────────────────────────────────────

_client: Optional[PreLendClient] = None


def get_client() -> PreLendClient:
    """Возвращает singleton PreLendClient (создаётся при первом вызове)."""
    global _client
    if _client is None:
        _client = PreLendClient()
    return _client
