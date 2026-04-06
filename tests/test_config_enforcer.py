"""
tests/test_config_enforcer.py — Тесты Zone 2 (visual) в config_enforcer (Сессия 11, ФИЧА 4).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


class TestApplySpVisual:
    """Тесты _apply_sp_visual: whitelist, запись config.json, обновление аккаунтов."""

    def _make_account(self, name: str, visual_filter: str = "none") -> Path:
        """Создаёт тестовый аккаунт в SP_ACCOUNTS_DIR."""
        import config
        acc_dir = config.SP_ACCOUNTS_DIR / name
        acc_dir.mkdir(parents=True, exist_ok=True)
        cfg = {"platforms": ["vk"], "visual_filter": visual_filter}
        (acc_dir / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        return acc_dir


    def test_allowed_filter_updates_config(self, init_database, monkeypatch):
        """Разрешённый фильтр записывается в account config.json."""
        import config
        from modules.config_enforcer import _apply_sp_visual

        self._make_account("acc_visual_test")
        cfg_path = config.SP_ACCOUNTS_DIR / "acc_visual_test" / "config.json"
        # Мокируем git, snapshot и save_applied_change (не нужны в unit-тесте)
        import integrations.git_tools as _gt
        monkeypatch.setattr(_gt, "commit_change", lambda **kw: True)
        import modules.agent_healer as _ah
        monkeypatch.setattr(_ah, "snapshot_config", lambda *a, **kw: None)
        import modules.config_enforcer as _ce
        monkeypatch.setattr(_ce, "save_applied_change", lambda **kw: 1)

        change = {
            "scope": "visual",
            "description": "тест cinematic",
            "accounts": ["acc_visual_test"],
            "param": "visual_filter",
            "new_value": "cinematic",
        }

        result = _apply_sp_visual(change, plan_id=1, zone="visual", description="тест")
        assert result is True

        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["visual_filter"] == "cinematic"

    def test_disallowed_filter_rejected(self, init_database):
        """Фильтр не из whitelist → возвращает False, config не меняется."""
        import config
        from modules.config_enforcer import _apply_sp_visual

        self._make_account("acc_bad_filter")
        cfg_path = config.SP_ACCOUNTS_DIR / "acc_bad_filter" / "config.json"
        original = cfg_path.read_text(encoding="utf-8")

        change = {
            "scope": "visual",
            "accounts": ["acc_bad_filter"],
            "param": "visual_filter",
            "new_value": "scale=0:0,exec=rm -rf /",  # инъекция ffmpeg
        }

        result = _apply_sp_visual(change, plan_id=1, zone="visual", description="")
        assert result is False
        assert cfg_path.read_text(encoding="utf-8") == original

    def test_same_filter_no_update(self, init_database):
        """Если фильтр не изменился — возвращает False (нечего обновлять)."""
        import config
        from modules.config_enforcer import _apply_sp_visual

        self._make_account("acc_same", visual_filter="warm")

        change = {
            "scope": "visual",
            "accounts": ["acc_same"],
            "param": "visual_filter",
            "new_value": "warm",
        }

        result = _apply_sp_visual(change, plan_id=1, zone="visual", description="")
        assert result is False

    def test_none_filter_allowed(self, init_database, monkeypatch):
        """'none' разрешён (отключает фильтр)."""
        import config
        from modules.config_enforcer import _apply_sp_visual
        import integrations.git_tools as _gt
        monkeypatch.setattr(_gt, "commit_change", lambda **kw: True)
        import modules.agent_healer as _ah
        monkeypatch.setattr(_ah, "snapshot_config", lambda *a, **kw: None)
        import modules.config_enforcer as _ce
        monkeypatch.setattr(_ce, "save_applied_change", lambda **kw: 1)

        self._make_account("acc_none_test", visual_filter="cinematic")

        change = {
            "scope": "visual",
            "accounts": ["acc_none_test"],
            "param": "visual_filter",
            "new_value": "none",
        }

        result = _apply_sp_visual(change, plan_id=1, zone="visual", description="сброс")
        assert result is True

        cfg_path = config.SP_ACCOUNTS_DIR / "acc_none_test" / "config.json"
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["visual_filter"] == "none"

    def test_empty_accounts_returns_false(self, init_database):
        """Если нет аккаунтов для обновления — возвращает False."""
        from modules.config_enforcer import _apply_sp_visual

        change = {
            "scope": "visual",
            "accounts": ["nonexistent_account_xyz"],
            "param": "visual_filter",
            "new_value": "warm",
        }

        result = _apply_sp_visual(change, plan_id=1, zone="visual", description="")
        assert result is False

    def test_whitelist_contains_expected_filters(self):
        """Whitelist содержит все фильтры из ФИЧИ 3."""
        from modules.config_enforcer import _SP_ALLOWED_VISUAL_FILTERS
        expected = {"cinematic", "warm", "cold", "vibrant", "muted",
                    "vhs", "sepia", "grayscale", "moody", "dreamy", "none"}
        for name in expected:
            assert name in _SP_ALLOWED_VISUAL_FILTERS, f"'{name}' не в whitelist"
