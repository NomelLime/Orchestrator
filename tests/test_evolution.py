"""
tests/test_evolution.py — Тесты генерации планов эволюции.

Покрывает:
    - _parse_plan: устойчивость к markdown, мусору до/после JSON
    - _build_prompt: все секции присутствуют
    - generate_plan: LLM замокана → план сохраняется в БД
    - Поведение при пустом/плохом ответе LLM
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Парсинг ответа LLM
# ─────────────────────────────────────────────────────────────────────────────

class TestParsePlan:
    """_parse_plan: устойчивый парсинг JSON из ответа LLM."""

    def _parse(self, raw: str):
        from modules.evolution import _parse_plan
        return _parse_plan(raw)

    def test_parses_clean_json(self):
        raw = '{"summary": "тест", "targets": {"zones": []}, "risk_assessment": {"estimated_risk": "low"}}'
        result = self._parse(raw)
        assert result is not None
        assert result["summary"] == "тест"

    def test_strips_markdown_code_block(self):
        """LLM часто оборачивает ответ в ```json ... ```."""
        raw = '```json\n{"summary": "тест", "targets": {}, "risk_assessment": {}}\n```'
        result = self._parse(raw)
        assert result is not None
        assert result["summary"] == "тест"

    def test_strips_markdown_without_lang(self):
        raw = '```\n{"summary": "тест2", "targets": {}, "risk_assessment": {}}\n```'
        result = self._parse(raw)
        assert result is not None

    def test_handles_preamble_text(self):
        """Текст перед JSON должен игнорироваться."""
        raw = 'Вот план:\n{"summary": "план", "targets": {}, "risk_assessment": {}}'
        result = self._parse(raw)
        assert result is not None
        assert result["summary"] == "план"

    def test_returns_none_on_empty(self):
        assert self._parse("") is None
        assert self._parse("нет JSON здесь") is None

    def test_returns_none_on_invalid_json(self):
        assert self._parse("{invalid json: }") is None

    def test_handles_nested_json(self):
        """Вложенная структура парсится корректно."""
        raw = """{
            "summary": "обновить расписание",
            "targets": {
                "zones": ["scheduling"],
                "shorts_project": {
                    "config_changes": [{"scope": "scheduling", "platform": "tiktok"}],
                    "code_patches": []
                }
            },
            "risk_assessment": {"estimated_risk": "low", "notes": "безопасно"}
        }"""
        result = self._parse(raw)
        assert result is not None
        assert result["targets"]["zones"] == ["scheduling"]
        assert result["risk_assessment"]["estimated_risk"] == "low"


# ─────────────────────────────────────────────────────────────────────────────
# Построение промпта
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildPrompt:
    """_build_prompt: все нужные секции присутствуют в промпте."""

    def test_contains_metrics_sections(self, init_database):
        from modules.evolution import _build_prompt

        metrics_data = {
            "shorts_project": {
                "period_hours": 24, "total_views": 1000, "total_likes": 50,
                "avg_ctr": 0.05, "top_platform": "youtube", "ban_count": 0,
                "agent_statuses": {}, "ab_summary": [], "raw_uploads": []
            },
            "prelend": {
                "period_hours": 24, "total_clicks": 500, "conversions": 10,
                "cr": 0.02, "bot_pct": 5.0, "top_geo": "BR",
                "shave_suspects": [], "analyst_verdicts": {}
            }
        }

        prompt = _build_prompt(metrics_data)

        assert "ShortsProject" in prompt
        assert "PreLend" in prompt
        assert "1000" in prompt      # total_views
        assert "youtube" in prompt   # top_platform
        assert "BR" in prompt        # top_geo

    def test_contains_zones_section(self, init_database):
        from modules.evolution import _build_prompt

        metrics_data = {
            "shorts_project": {"period_hours": 24, "total_views": 0, "total_likes": 0,
                               "avg_ctr": None, "top_platform": None, "ban_count": 0,
                               "agent_statuses": {}, "ab_summary": [], "raw_uploads": []},
            "prelend": {"period_hours": 24, "total_clicks": 0, "conversions": 0,
                        "cr": None, "bot_pct": None, "top_geo": None,
                        "shave_suspects": [], "analyst_verdicts": {}}
        }

        prompt = _build_prompt(metrics_data)
        assert "ДОСТУПНЫЕ ЗОНЫ" in prompt
        assert "scheduling" in prompt
        assert "visual" in prompt

    def test_contains_format_instructions(self, init_database):
        """Инструкции по формату JSON всегда присутствуют."""
        from modules.evolution import _build_prompt

        metrics_data = {
            "shorts_project": {"period_hours": 24, "total_views": 0, "total_likes": 0,
                               "avg_ctr": None, "top_platform": None, "ban_count": 0,
                               "agent_statuses": {}, "ab_summary": [], "raw_uploads": []},
            "prelend": {"period_hours": 24, "total_clicks": 0, "conversions": 0,
                        "cr": None, "bot_pct": None, "top_geo": None,
                        "shave_suspects": [], "analyst_verdicts": {}}
        }

        prompt = _build_prompt(metrics_data)
        assert "estimated_risk" in prompt
        assert "config_changes" in prompt


# ─────────────────────────────────────────────────────────────────────────────
# generate_plan (интеграционный, LLM замокана)
# ─────────────────────────────────────────────────────────────────────────────

class TestGeneratePlan:
    """generate_plan: LLM замокана, план сохраняется в БД."""

    _GOOD_LLM_RESPONSE = """{
        "summary": "Сдвинуть расписание TikTok на вечер",
        "targets": {
            "zones": ["scheduling"],
            "shorts_project": {
                "config_changes": [
                    {
                        "scope": "scheduling",
                        "description": "TikTok вечернее расписание",
                        "accounts": ["all"],
                        "platform": "tiktok",
                        "new_value": ["20:00", "22:00"]
                    }
                ],
                "code_patches": []
            },
            "prelend": {"config_changes": [], "code_patches": []}
        },
        "risk_assessment": {"estimated_risk": "low", "notes": "Безопасно"}
    }"""

    def test_plan_saved_to_db(self, init_database):
        """При успешном ответе LLM план сохраняется в evolution_plans."""
        from modules.evolution import generate_plan
        from db.connection import get_db

        metrics_data = {
            "shorts_project": {"period_hours": 24, "total_views": 100, "total_likes": 5,
                               "avg_ctr": 0.05, "top_platform": "tiktok", "ban_count": 0,
                               "agent_statuses": {}, "ab_summary": [], "raw_uploads": []},
            "prelend": {"period_hours": 24, "total_clicks": 50, "conversions": 1,
                        "cr": 0.02, "bot_pct": 5.0, "top_geo": "BR",
                        "shave_suspects": [], "analyst_verdicts": {}}
        }

        with patch("modules.evolution.call_llm", return_value=self._GOOD_LLM_RESPONSE):
            plan = generate_plan(metrics_data)

        assert plan is not None
        assert "_plan_id" in plan
        assert plan["summary"] == "Сдвинуть расписание TikTok на вечер"

        # Проверяем что план записан в БД
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM evolution_plans WHERE id = ?", (plan["_plan_id"],)
            ).fetchone()
        assert row is not None
        assert row["risk_level"] == "low"
        assert row["status"] == "pending"

    def test_returns_none_on_bad_llm_response(self, init_database):
        """При мусорном ответе LLM возвращает None (без исключений)."""
        from modules.evolution import generate_plan

        metrics_data = {
            "shorts_project": {"period_hours": 24, "total_views": 0, "total_likes": 0,
                               "avg_ctr": None, "top_platform": None, "ban_count": 0,
                               "agent_statuses": {}, "ab_summary": [], "raw_uploads": []},
            "prelend": {"period_hours": 24, "total_clicks": 0, "conversions": 0,
                        "cr": None, "bot_pct": None, "top_geo": None,
                        "shave_suspects": [], "analyst_verdicts": {}}
        }

        with patch("modules.evolution.call_llm", return_value="Не могу помочь с этим"):
            plan = generate_plan(metrics_data)

        assert plan is None

    def test_returns_none_when_llm_unavailable(self, init_database):
        """Если LLM недоступна (call_llm → None) → plan = None."""
        from modules.evolution import generate_plan

        metrics_data = {
            "shorts_project": {"period_hours": 24, "total_views": 0, "total_likes": 0,
                               "avg_ctr": None, "top_platform": None, "ban_count": 0,
                               "agent_statuses": {}, "ab_summary": [], "raw_uploads": []},
            "prelend": {"period_hours": 24, "total_clicks": 0, "conversions": 0,
                        "cr": None, "bot_pct": None, "top_geo": None,
                        "shave_suspects": [], "analyst_verdicts": {}}
        }

        with patch("modules.evolution.call_llm", return_value=None):
            plan = generate_plan(metrics_data)

        assert plan is None
