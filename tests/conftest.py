"""
tests/conftest.py — Общие фикстуры для тестов Orchestrator.

Все тесты используют временные файлы и in-memory SQLite —
реальные ShortsProject/PreLend файлы не трогаются.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

# Добавляем корень проекта в sys.path чтобы импорты работали без установки пакета
sys.path.insert(0, str(Path(__file__).parent.parent))


# ─────────────────────────────────────────────────────────────────────────────
# Патчинг config — подменяем пути на временные директории
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def patch_config(tmp_path, monkeypatch):
    """
    Подменяет все пути в config на временные директории.
    autouse=True → применяется ко всем тестам автоматически.
    """
    import config

    # БД Orchestrator — в памяти через tmp_path
    monkeypatch.setattr(config, "DB_PATH",             tmp_path / "orchestrator.db")
    monkeypatch.setattr(config, "CYCLE_LOCK_FILE",     tmp_path / ".cycle.lock")

    # Пути к управляемым проектам — tmp_path (не реальные репозитории)
    monkeypatch.setattr(config, "SHORTS_PROJECT_DIR",  tmp_path / "ShortsProject")
    monkeypatch.setattr(config, "PRELEND_DIR",         tmp_path / "PreLend")
    monkeypatch.setattr(config, "SP_ANALYTICS_FILE",   tmp_path / "ShortsProject" / "data" / "analytics.json")
    monkeypatch.setattr(config, "SP_AGENT_MEMORY",     tmp_path / "ShortsProject" / "data" / "agent_memory.json")
    monkeypatch.setattr(config, "SP_ACCOUNTS_DIR",     tmp_path / "ShortsProject" / "accounts")
    monkeypatch.setattr(config, "PL_CLICKS_DB",        tmp_path / "PreLend" / "data" / "clicks.db")
    monkeypatch.setattr(config, "PL_AGENT_MEMORY",     tmp_path / "PreLend" / "data" / "agent_memory.json")
    monkeypatch.setattr(config, "PL_SHAVE_REPORT",     tmp_path / "PreLend" / "data" / "shave_report.json")
    monkeypatch.setattr(config, "PL_SETTINGS",         tmp_path / "PreLend" / "config" / "settings.json")
    monkeypatch.setattr(config, "PL_ADVERTISERS",      tmp_path / "PreLend" / "config" / "advertisers.json")

    # Безопасные значения
    monkeypatch.setattr(config, "DRY_RUN",        True)
    monkeypatch.setattr(config, "GIT_AUTOCOMMIT", False)

    # Создаём нужные директории
    (tmp_path / "ShortsProject" / "data").mkdir(parents=True)
    (tmp_path / "ShortsProject" / "accounts").mkdir(parents=True)
    (tmp_path / "PreLend" / "data").mkdir(parents=True)
    (tmp_path / "PreLend" / "config").mkdir(parents=True)


@pytest.fixture
def init_database():
    """Инициализирует SQLite БД Orchestrator (создаёт все таблицы)."""
    from db.connection import init_db
    init_db()


# ─────────────────────────────────────────────────────────────────────────────
# Фабрики тестовых данных
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def make_sp_analytics(tmp_path):
    """Фабрика: создаёт analytics.json в tmp_path с заданными данными."""
    def _make(data: dict):
        path = tmp_path / "ShortsProject" / "data" / "analytics.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path
    return _make


@pytest.fixture
def make_sp_memory(tmp_path):
    """Фабрика: создаёт agent_memory.json для ShortsProject."""
    def _make(kv: dict = None, agents: dict = None):
        path = tmp_path / "ShortsProject" / "data" / "agent_memory.json"
        path.write_text(json.dumps({
            "kv":     kv or {},
            "agents": agents or {},
        }), encoding="utf-8")
        return path
    return _make


@pytest.fixture
def make_prelend_db(tmp_path):
    """
    Фабрика: создаёт clicks.db с реальной схемой PreLend и вставляет тестовые данные.
    Схема совпадает с PreLend/data/init_db.sql.
    """
    import sqlite3

    def _make(clicks: list = None, conversions: list = None):
        db_path = tmp_path / "PreLend" / "data" / "clicks.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            PRAGMA journal_mode = WAL;

            CREATE TABLE IF NOT EXISTS clicks (
                click_id        TEXT PRIMARY KEY,
                ts              INTEGER NOT NULL,
                ip              TEXT,
                geo             TEXT,
                device          TEXT,
                platform        TEXT,
                advertiser_id   TEXT,
                utm_source      TEXT,
                utm_medium      TEXT,
                utm_campaign    TEXT,
                utm_content     TEXT,
                utm_term        TEXT,
                ua_hash         TEXT,
                referer         TEXT,
                is_test         INTEGER NOT NULL DEFAULT 0,
                status          TEXT NOT NULL DEFAULT 'sent'
            );

            CREATE TABLE IF NOT EXISTS conversions (
                conv_id         TEXT PRIMARY KEY,
                date            TEXT NOT NULL,
                advertiser_id   TEXT NOT NULL,
                count           INTEGER NOT NULL DEFAULT 1,
                source          TEXT NOT NULL DEFAULT 'manual',
                notes           TEXT,
                created_at      INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS landing_status (
                advertiser_id   TEXT PRIMARY KEY,
                last_check      INTEGER,
                response_ms     INTEGER,
                is_up           INTEGER NOT NULL DEFAULT 1,
                uptime_24h      REAL NOT NULL DEFAULT 100.0
            );

            CREATE TABLE IF NOT EXISTS advertiser_rates (
                advertiser_id   TEXT PRIMARY KEY,
                rate            REAL NOT NULL DEFAULT 0.0,
                currency        TEXT NOT NULL DEFAULT 'USD',
                updated_at      INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_clicks_ts        ON clicks (ts);
            CREATE INDEX IF NOT EXISTS idx_clicks_status    ON clicks (status);
            CREATE INDEX IF NOT EXISTS idx_clicks_geo       ON clicks (geo);
            CREATE INDEX IF NOT EXISTS idx_conversions_date ON conversions (date);
        """)

        now_ts = int(time.time())

        for click in (clicks or []):
            conn.execute(
                "INSERT INTO clicks "
                "(click_id, ts, ip, geo, device, platform, advertiser_id, "
                " utm_source, utm_medium, utm_campaign, utm_content, utm_term, "
                " ua_hash, referer, is_test, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    click.get("click_id", f"test_{now_ts}_{id(click)}"),
                    click.get("ts", now_ts),
                    click.get("ip"),
                    click.get("geo", "BR"),
                    click.get("device"),
                    click.get("platform", "youtube"),
                    click.get("advertiser_id"),
                    click.get("utm_source"),
                    click.get("utm_medium"),
                    click.get("utm_campaign"),
                    click.get("utm_content"),
                    click.get("utm_term"),
                    click.get("ua_hash"),
                    click.get("referer"),
                    click.get("is_test", 0),
                    click.get("status", "sent"),
                )
            )

        for i, conv in enumerate(conversions or []):
            conn.execute(
                "INSERT INTO conversions (conv_id, date, advertiser_id, count, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    conv.get("conv_id", f"conv_{i}"),
                    conv.get("date", "2024-01-01"),
                    conv.get("advertiser_id", "adv1"),
                    conv.get("count", 1),
                    conv.get("created_at", now_ts),
                )
            )

        conn.commit()
        conn.close()
        return db_path

    return _make
