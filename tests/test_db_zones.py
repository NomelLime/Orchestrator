"""
tests/test_db_zones.py — Тесты таблицы zones и логики confidence_score.

Покрывает:
    - Инициализация: 4 зоны созданы с правильными начальными значениями
    - update_zone_score: рост/падение в пределах 0-100
    - Hysteresis: включение при >= ACTIVATE_THRESHOLD, выключение при < DEACTIVATE_THRESHOLD
    - apply_zone_decay: деградация после ZONE_DECAY_DAYS без применения
    - set_zone_enabled: принудительное включение/выключение
    - is_zone_frozen: политика заморозки
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest


class TestZoneInitialization:
    """После init_db все 4 зоны существуют с правильными начальными значениями."""

    def test_all_zones_exist(self, init_database):
        from db.zones import get_all_zones
        zones = get_all_zones()
        assert set(zones.keys()) == {"scheduling", "visual", "prelend", "code"}

    def test_scheduling_enabled_by_default(self, init_database):
        from db.zones import get_zone
        z = get_zone("scheduling")
        assert z["enabled"] == 1
        assert z["confidence_score"] == 70

    def test_other_zones_disabled_by_default(self, init_database):
        from db.zones import get_zone
        for name in ("visual", "prelend", "code"):
            z = get_zone(name)
            assert z["enabled"] == 0, f"Зона {name} должна быть выключена по умолчанию"

    def test_code_zone_lowest_score(self, init_database):
        from db.zones import get_zone
        z = get_zone("code")
        assert z["confidence_score"] == 20


class TestZoneScoreUpdate:
    """update_zone_score: изменение score и автоматическое переключение."""

    def test_score_increases_on_success(self, init_database):
        from db.zones import get_zone, update_zone_score
        import config

        before = get_zone("scheduling")["confidence_score"]
        update_zone_score("scheduling", delta=+config.ZONE_SCORE_SUCCESS_DELTA)
        after = get_zone("scheduling")["confidence_score"]
        assert after == before + config.ZONE_SCORE_SUCCESS_DELTA

    def test_score_decreases_on_failure(self, init_database):
        from db.zones import get_zone, update_zone_score
        import config

        before = get_zone("scheduling")["confidence_score"]
        update_zone_score("scheduling", delta=-config.ZONE_SCORE_FAILURE_DELTA)
        after = get_zone("scheduling")["confidence_score"]
        assert after == before - config.ZONE_SCORE_FAILURE_DELTA

    def test_score_clamped_at_100(self, init_database):
        from db.zones import get_zone, update_zone_score
        update_zone_score("scheduling", delta=+200)
        z = get_zone("scheduling")
        assert z["confidence_score"] <= 100

    def test_score_clamped_at_zero(self, init_database):
        from db.zones import get_zone, update_zone_score
        update_zone_score("code", delta=-200)
        z = get_zone("code")
        assert z["confidence_score"] >= 0

    def test_zone_activates_at_threshold(self, init_database):
        """visual начинает с score=50 (выключена). После +25 → score=75 >= 70 → включается."""
        from db.zones import get_zone, update_zone_score
        import config

        z = get_zone("visual")
        assert z["enabled"] == 0
        assert z["confidence_score"] == 50

        # Поднимаем до >= ACTIVATE_THRESHOLD (70)
        update_zone_score("visual", delta=+25)
        z = get_zone("visual")
        assert z["enabled"] == 1, "Зона должна включиться при score >= 70"

    def test_zone_deactivates_below_threshold(self, init_database):
        """scheduling начинает с score=70 (включена). После -45 → score=25 < 30 → выключается."""
        from db.zones import get_zone, update_zone_score
        import config

        z = get_zone("scheduling")
        assert z["enabled"] == 1

        # Роняем ниже DEACTIVATE_THRESHOLD (30)
        update_zone_score("scheduling", delta=-45)
        z = get_zone("scheduling")
        assert z["enabled"] == 0, "Зона должна выключиться при score < 30"

    def test_hysteresis_no_oscillation(self, init_database):
        """
        Зона выключена при score=35 (между 30 и 70).
        Ни включение ни выключение не должно произойти.
        """
        from db.zones import get_zone, update_zone_score, set_zone_enabled

        # Принудительно выключаем visual
        set_zone_enabled("visual", False)
        # Устанавливаем score=35 (между порогами)
        from db.connection import get_db
        with get_db() as conn:
            conn.execute("UPDATE zones SET confidence_score = 35 WHERE zone_name = 'visual'")

        # delta=0 → score остаётся 35 → зона остаётся выключенной
        update_zone_score("visual", delta=0)
        z = get_zone("visual")
        assert z["enabled"] == 0, "При score=35 зона не должна включиться (hysteresis)"

    def test_mark_applied_updates_timestamp(self, init_database):
        from db.zones import get_zone, update_zone_score
        z_before = get_zone("scheduling")
        assert z_before["last_applied_at"] is None

        update_zone_score("scheduling", delta=+1, mark_applied=True)
        z_after = get_zone("scheduling")
        assert z_after["last_applied_at"] is not None


class TestZoneDecay:
    """apply_zone_decay: пассивное снижение score для давно неактивных зон."""

    def test_no_decay_for_recent_zone(self, init_database, monkeypatch):
        """Если зона применялась недавно — score не снижается."""
        from db.zones import get_zone, update_zone_score, apply_zone_decay

        # Помечаем как применявшуюся только что
        update_zone_score("scheduling", delta=0, mark_applied=True)
        before = get_zone("scheduling")["confidence_score"]

        apply_zone_decay()
        after = get_zone("scheduling")["confidence_score"]
        assert after == before

    def test_decay_after_threshold(self, init_database):
        """Если зона не применялась > ZONE_DECAY_DAYS — score снижается."""
        import config
        from db.zones import get_zone, apply_zone_decay
        from db.connection import get_db

        # Симулируем что зона применялась давно (> DECAY_DAYS)
        old_ts = (
            datetime.now() - timedelta(days=config.ZONE_DECAY_DAYS + 3)
        ).isoformat()
        with get_db() as conn:
            conn.execute(
                "UPDATE zones SET last_applied_at = ? WHERE zone_name = 'scheduling'",
                (old_ts,)
            )

        before = get_zone("scheduling")["confidence_score"]
        # Сбрасываем guard чтобы decay выполнился в тесте
        import db.zones as _zones_mod
        _zones_mod._last_decay_date = None
        apply_zone_decay()
        after = get_zone("scheduling")["confidence_score"]

        assert after < before, "Score должен снизиться после деградации"
        # 3 дня сверх порога → decay = 3 * ZONE_DECAY_PER_DAY
        expected_decay = 3 * config.ZONE_DECAY_PER_DAY
        assert after == max(0, before - expected_decay)


class TestZoneFreezing:
    """is_zone_frozen и set_zone_enabled через политики оператора."""

    def test_zone_not_frozen_by_default(self, init_database):
        from db.zones import is_zone_active
        from db.commands import is_zone_frozen
        # scheduling включена и не заморожена
        assert not is_zone_frozen("scheduling")

    def test_zone_frozen_via_policy(self, init_database):
        from db.commands import set_policy, is_zone_frozen
        set_policy("freeze_zone_visual", True)
        assert is_zone_frozen("visual")

    def test_zone_unfrozen_via_policy(self, init_database):
        from db.commands import set_policy, is_zone_frozen
        set_policy("freeze_zone_visual", True)
        set_policy("freeze_zone_visual", False)
        assert not is_zone_frozen("visual")
