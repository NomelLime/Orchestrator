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
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import requests

logger = logging.getLogger(__name__)

_PL_API_URL = os.getenv("PL_INTERNAL_API_URL", "http://localhost:9090")
_PL_API_KEY = os.getenv("PL_INTERNAL_API_KEY", "")
_TIMEOUT    = int(os.getenv("PL_INTERNAL_TIMEOUT", "10"))


def _agent_log(hypothesis_id: str, location: str, message: str, data: dict) -> None:
    # region agent log
    try:
        payload = {
            "sessionId": "0398bc",
            "runId": "pre-fix",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        log_path = Path(__file__).resolve().parents[2] / "debug-0398bc.log"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
    # endregion


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
        # region agent log
        _agent_log(
            "H7",
            "Orchestrator/integrations/prelend_client.py:__init__",
            "PreLendClient initialized",
            {
                "baseUrl": self._base,
                "apiKeyPresent": bool(api_key),
                "apiKeyLength": len(api_key or ""),
            },
        )
        # endregion
        if api_key:
            self._session.headers["X-API-Key"] = api_key
        # region agent log
        _agent_log(
            "H7",
            "Orchestrator/integrations/prelend_client.py:__init__",
            "Session header prepared",
            {
                "hasXApiKeyHeader": "X-API-Key" in self._session.headers,
                "headerLength": len(self._session.headers.get("X-API-Key", "")),
            },
        )
        # endregion

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
            # region agent log
            _agent_log(
                "H8",
                "Orchestrator/integrations/prelend_client.py:_get",
                "Sending GET to PreLend API",
                {
                    "path": path,
                    "baseUrl": self._base,
                    "hasXApiKeyHeader": bool(self._session.headers.get("X-API-Key")),
                },
            )
            # endregion
            r = self._session.get(
                f"{self._base}{path}",
                params=params,
                timeout=self._timeout,
            )
            # region agent log
            _agent_log(
                "H8",
                "Orchestrator/integrations/prelend_client.py:_get",
                "Received response from PreLend API",
                {"path": path, "statusCode": r.status_code},
            )
            # endregion
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
        try:
            r = self._session.put(
                f"{self._base}{path}",
                json=json_body,
                params=params,
                timeout=self._timeout,
            )
            r.raise_for_status()
            return True
        except requests.ConnectionError:
            logger.error(
                "[PreLendClient] Нет связи с PreLend API при PUT %s. "
                "Изменение конфига НЕ применено.",
                path,
            )
            return False
        except Exception as exc:
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
