"""
utils/llm_json.py — Извлечение JSON из ответов LLM.

[FIX] DRY: ранее идентичная логика дублировалась в evolution.py::_parse_plan()
и evaluator.py::_parse_llm_json_obj(). Теперь единственная реализация.

Экспортирует:
    extract_json_object(raw: str) → Optional[dict]
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


def extract_json_object(raw: str) -> Optional[dict]:
    """Извлекает первый сбалансированный JSON-объект из ответа LLM.

    Устойчив к:
        - markdown-обёрткам (```json ... ```)
        - мусору до/после JSON
        - скобкам внутри JSON-строк (не влияют на счётчик глубины)

    Args:
        raw: сырой текст ответа LLM

    Returns:
        dict если найден валидный JSON, иначе None
    """
    if not raw:
        return None

    # Убираем ```json ... ``` обёртки
    clean = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

    start = clean.find("{")
    if start == -1:
        return None

    depth = 0
    in_str = False
    escaped = False

    for i, ch in enumerate(clean[start:], start):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_str:
            escaped = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(clean[start : i + 1])
                except json.JSONDecodeError as exc:
                    logger.warning("[llm_json] JSON parse error: %s", exc)
                    return None

    return None
