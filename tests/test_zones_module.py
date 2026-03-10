"""
tests/test_zones_module.py — Тесты модуля modules/zones.py.

Покрывает:
    - can_apply: DRY_RUN, freeze, disabled zone, Zone 4 зависимость от Zone 2
    - record_success/failure: изменение score
    - get_zones_summary: строка для Telegram
"""

from __future__ import annotations

from unittest.mock import patch


class TestCanApply:
    """can_apply: основной контролёр допуска перед изменениями."""

    def test_dry_run_blocks_all(self, init_database):
        """DRY_RUN=True блокирует любую зону."""
        import config
        from modules.zones import can_apply

        # config уже пропатчен на DRY_RUN=True через conftest.py
        assert config.DRY_RUN is True
        assert not can_apply("scheduling")
        assert not can_apply("visual")

    def test_active_zone_allowed_without_dry_run(self, init_database, monkeypatch):
        """scheduling активна (score=70) и не заморожена → can_apply=True."""
        import config
        monkeypatch.setattr(config, "DRY_RUN", False)

        from modules.zones import can_apply
        assert can_apply("scheduling")

    def test_inactive_zone_blocked(self, init_database, monkeypatch):
        """visual выключена по умолчанию → can_apply=False."""
        import config
        monkeypatch.setattr(config, "DRY_RUN", False)

        from modules.zones import can_apply
        assert not can_apply("visual")

    def test_frozen_zone_blocked(self, init_database, monkeypatch):
        """Замороженная оператором зона блокируется даже если enabled=True."""
        import config
        monkeypatch.setattr(config, "DRY_RUN", False)

        from db.commands import set_policy
        set_policy("freeze_zone_scheduling", True)

        from modules.zones import can_apply
        assert not can_apply("scheduling")

    def test_code_zone_blocked_if_visual_inactive(self, init_database, monkeypatch):
        """Zone 4 (code) требует активную Zone 2 (visual). Если visual выключена — code тоже."""
        import config
        monkeypatch.setattr(config, "DRY_RUN", False)

        # Включаем code принудительно
        from db.zones import set_zone_enabled
        set_zone_enabled("code", True)
        # visual остаётся выключенной (по умолчанию)

        from modules.zones import can_apply
        assert not can_apply("code")

    def test_code_zone_allowed_if_visual_active(self, init_database, monkeypatch):
        """Zone 4 разрешена если Zone 2 активна."""
        import config
        monkeypatch.setattr(config, "DRY_RUN", False)

        from db.zones import set_zone_enabled
        set_zone_enabled("visual", True)
        set_zone_enabled("code", True)

        from modules.zones import can_apply
        assert can_apply("code")


class TestRecordSuccessFailure:
    """record_success/failure: изменение confidence_score."""

    def test_record_success_increases_score(self, init_database):
        from db.zones import get_zone
        from modules.zones import record_success
        import config

        before = get_zone("scheduling")["confidence_score"]
        record_success("scheduling", "тестовое изменение")
        after = get_zone("scheduling")["confidence_score"]

        assert after == min(100, before + config.ZONE_SCORE_SUCCESS_DELTA)

    def test_record_failure_decreases_score(self, init_database):
        from db.zones import get_zone
        from modules.zones import record_failure
        import config

        before = get_zone("scheduling")["confidence_score"]
        record_failure("scheduling", "тест упал")
        after = get_zone("scheduling")["confidence_score"]

        assert after == max(0, before - config.ZONE_SCORE_FAILURE_DELTA)

    def test_record_success_marks_applied(self, init_database):
        """record_success обновляет last_applied_at (сбрасывает деградацию)."""
        from db.zones import get_zone
        from modules.zones import record_success

        record_success("scheduling")
        z = get_zone("scheduling")
        assert z["last_applied_at"] is not None


class TestGetZonesSummary:
    """get_zones_summary: строка для Telegram содержит все зоны."""

    def test_summary_contains_all_zones(self, init_database):
        from modules.zones import get_zones_summary
        summary = get_zones_summary()

        for zone in ("scheduling", "visual", "prelend", "code"):
            assert zone in summary

    def test_summary_shows_scores(self, init_database):
        from modules.zones import get_zones_summary
        summary = get_zones_summary()
        # scheduling имеет score 70
        assert "70" in summary

    def test_summary_shows_frozen_marker(self, init_database):
        from db.commands import set_policy
        from modules.zones import get_zones_summary

        set_policy("freeze_zone_visual", True)
        summary = get_zones_summary()
        assert "🔒" in summary
