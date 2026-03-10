-- schema.sql — SQLite схема базы опыта Orchestrator.
--
-- Принципы:
--   1. Все таблицы создаются через IF NOT EXISTS → безопасен повторный запуск.
--   2. Timestamps хранятся как TEXT в ISO-8601 формате (читаемы человеком).
--   3. JSON-поля хранятся как TEXT (SQLite не имеет нативного JSON-типа).
--   4. Все FK прописаны для документирования связей, но PRAGMA foreign_keys
--      включается в connection.py при каждом подключении.

PRAGMA journal_mode = WAL;        -- Позволяет читать БД пока идёт запись
PRAGMA foreign_keys = ON;

-- ─────────────────────────────────────────────────────────────────────────────
-- Таблица 1: Зоны влияния Orchestrator
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS zones (
    zone_name           TEXT PRIMARY KEY,   -- 'scheduling' | 'visual' | 'prelend' | 'code'
    enabled             INTEGER NOT NULL DEFAULT 0,     -- 0/1 (bool)
    confidence_score    INTEGER NOT NULL DEFAULT 50,    -- 0-100
    last_applied_at     TEXT,               -- ISO timestamp последнего применённого плана
    last_changed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    -- Для расчёта пассивной деградации confidence_score:
    -- если (now - last_applied_at) > ZONE_DECAY_DAYS → score -= ZONE_DECAY_PER_DAY
    notes               TEXT                -- произвольный комментарий оператора
);

-- Начальные значения зон (вставка при первой инициализации)
INSERT OR IGNORE INTO zones (zone_name, enabled, confidence_score) VALUES
    ('scheduling', 1, 70),   -- Zone 1: сразу включена, достаточно доверия
    ('visual',     0, 50),   -- Zone 2: выключена, нужно накопить опыт
    ('prelend',    0, 30),   -- Zone 3: выключена, зависит от Zone 2
    ('code',       0, 20);   -- Zone 4: выключена, самая опасная

-- ─────────────────────────────────────────────────────────────────────────────
-- Таблица 2: Планы эволюции (выход LLM-анализа)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS evolution_plans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    summary         TEXT NOT NULL,          -- краткое описание плана (для Telegram)
    raw_plan_json   TEXT NOT NULL,          -- полный JSON плана (формат из config 6)
    zones_affected  TEXT,                   -- JSON-список зон: ["scheduling", "visual"]
    files_affected  TEXT,                   -- JSON-список файлов
    risk_level      TEXT NOT NULL DEFAULT 'low',  -- 'low' | 'medium' | 'high'
    status          TEXT NOT NULL DEFAULT 'pending'  -- 'pending' | 'applied' | 'skipped' | 'failed'
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Таблица 3: Применённые изменения (результат исполнения плана)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS applied_changes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    evolution_plan_id   INTEGER REFERENCES evolution_plans(id),
    applied_at          TEXT NOT NULL DEFAULT (datetime('now')),
    change_type         TEXT NOT NULL,      -- 'config_change' | 'code_patch'
    repo                TEXT NOT NULL,      -- 'ShortsProject' | 'PreLend'
    file_path           TEXT,               -- относительный путь к изменённому файлу
    zone                TEXT,               -- в какой зоне это изменение
    description         TEXT,               -- человекочитаемое описание
    -- Для config_change: что было → что стало
    old_value_json      TEXT,               -- JSON предыдущего значения
    new_value_json      TEXT,               -- JSON нового значения
    -- Для code_patch: результат тестов
    test_status         TEXT,               -- 'passed' | 'failed' | 'skipped'
    test_output         TEXT,               -- вывод pytest (первые 2000 символов)
    rolled_back         INTEGER DEFAULT 0,  -- 0/1 — был ли откат
    rollback_reason     TEXT,               -- причина отката
    -- Влияние на метрики (заполняется позже, когда накопится статистика)
    metric_impact_json  TEXT                -- {"views_delta_pct": 5.2, ...}
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Таблица 4: Команды оператора (из Telegram)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS operator_commands (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at     TEXT NOT NULL DEFAULT (datetime('now')),
    source          TEXT NOT NULL DEFAULT 'telegram',
    raw_text        TEXT NOT NULL,          -- оригинальный текст сообщения
    parsed_json     TEXT,                   -- структурированная интерпретация LLM
    command_type    TEXT,                   -- 'policy_update' | 'manual_action' | 'config_hint'
    status          TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'applied' | 'rejected'
    applied_at      TEXT,
    notes           TEXT
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Таблица 5: Активные политики оператора
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS operator_policies (
    key             TEXT PRIMARY KEY,       -- например: 'freeze_zone_visual', 'focus_geo'
    value_json      TEXT NOT NULL,          -- JSON-значение политики
    set_at          TEXT NOT NULL DEFAULT (datetime('now')),
    set_by_command  INTEGER REFERENCES operator_commands(id),
    expires_at      TEXT,                   -- NULL = бессрочно
    description     TEXT
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Таблица 6: Снапшоты метрик (для трекинга и сравнения до/после)
-- ─────────────────────────────────────────────────────────────────────────────
-- Источники:
--   ShortsProject → analytics.json (views, likes, comments, A/B winner)
--   PreLend       → clicks.db      (clicks, conversions, CR, bot_pct, geo)

CREATE TABLE IF NOT EXISTS metrics_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at     TEXT NOT NULL DEFAULT (datetime('now')),
    source          TEXT NOT NULL,          -- 'ShortsProject' | 'PreLend'
    period_hours    INTEGER NOT NULL DEFAULT 24,  -- за какой период собрано
    -- ShortsProject метрики
    sp_total_views  INTEGER,
    sp_total_likes  INTEGER,
    sp_avg_ctr      REAL,                   -- средний CTR по A/B вариантам
    sp_top_platform TEXT,                   -- платформа с лучшими показателями
    sp_ab_winner    TEXT,                   -- текущий победитель A/B (variant label)
    sp_ban_count    INTEGER,                -- количество бан-событий за период
    -- PreLend метрики
    pl_total_clicks INTEGER,
    pl_conversions  INTEGER,
    pl_cr           REAL,                   -- conversion rate
    pl_bot_pct      REAL,                   -- процент ботов
    pl_top_geo      TEXT,                   -- топ ГЕО по кликам
    -- Сырые данные для детального анализа LLM
    raw_summary_json TEXT
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Таблица 7: Уведомления (буфер для суточной сводки)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    level           TEXT NOT NULL DEFAULT 'info',   -- 'info' | 'warning' | 'error'
    category        TEXT,                   -- 'plan' | 'zone' | 'patch' | 'rollback' | 'metric'
    message         TEXT NOT NULL,
    included_in_digest INTEGER DEFAULT 0,   -- 0/1 — уже вошло в суточную сводку
    digest_date     TEXT                    -- дата сводки, в которую включено
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Индексы для часто используемых запросов
-- ─────────────────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_evolution_plans_status
    ON evolution_plans(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_applied_changes_plan
    ON applied_changes(evolution_plan_id, applied_at DESC);

CREATE INDEX IF NOT EXISTS idx_metrics_snapshots_time
    ON metrics_snapshots(snapshot_at DESC, source);

CREATE INDEX IF NOT EXISTS idx_notifications_digest
    ON notifications(included_in_digest, created_at DESC);
