"""
integrations/ollama_client.py — Обёртка над локальными LLM (Ollama).

Использует библиотеку ollama (pip install ollama).
Обрабатывает ошибки соединения, таймауты и возвращает None вместо исключений —
Orchestrator должен работать даже если LLM временно недоступна.

Экспортирует:
    call_llm(model, prompt) → str или None
    is_ollama_available()   → bool
"""

from __future__ import annotations

import logging
from typing import Optional

import config
from integrations.shared_gpu_lock import acquire_gpu_lock

logger = logging.getLogger(__name__)


def call_llm(model: str, prompt: str) -> Optional[str]:
    """
    Отправляет промпт в Ollama и возвращает текст ответа.
    Возвращает None при любой ошибке (недоступность, таймаут, пустой ответ).

    Использует cross-project GPU lock (shared_gpu_lock.py) чтобы не конкурировать
    с ShortsProject VL-активностью за VRAM.
    """
    try:
        import ollama as _ollama

        with acquire_gpu_lock(consumer=f"Orchestrator-{model}", timeout=120):
            response = _ollama.generate(
                model   = model,
                prompt  = prompt,
                options = {
                    "temperature": 0.3,     # низкая температура = более детерминированный вывод
                    "num_predict": 2048,    # лимит токенов ответа
                },
            )

        raw = (response.get("response") or "").strip()
        if not raw:
            logger.warning("[Ollama] Пустой ответ от модели %s", model)
            return None

        logger.debug("[Ollama] Ответ от %s: %d символов", model, len(raw))
        return raw

    except ImportError:
        logger.error("[Ollama] Библиотека ollama не установлена. pip install ollama")
        return None

    except Exception as exc:
        # Не даём исключению всплыть наверх — Orchestrator продолжит цикл
        logger.warning("[Ollama] Ошибка вызова %s: %s", model, exc)
        return None


def is_ollama_available() -> bool:
    """Проверяет доступность Ollama через простой запрос."""
    try:
        import requests
        resp = requests.get(f"{config.OLLAMA_HOST}/api/tags", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False
