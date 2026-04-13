# Migration: Agent Events Registry (`orchestrator.db`)

Добавляет/выравнивает таблицу `agent_events` для сквозной аналитики:

- `source_project`
- `agent_name`
- `event_type`
- `severity`
- `creative_id`
- `hook_type`
- `experiment_id`
- `agent_run_id`
- `payload_json`
- `created_at`

## 1) Бэкап БД

```bash
cp /path/to/Orchestrator/data/orchestrator.db "/var/backups/orchestrator.db.$(date +%Y%m%d_%H%M%S)"
```

## 2) Применение миграции (безопасно, можно повторять)

```bash
cd /path/to/Orchestrator
python scripts/migrate_orchestrator_db.py --db /path/to/Orchestrator/data/orchestrator.db
```

## 3) Проверка структуры таблицы

```bash
sqlite3 /path/to/Orchestrator/data/orchestrator.db "PRAGMA table_info(agent_events);"
```

## 4) Проверка индексов

```bash
sqlite3 /path/to/Orchestrator/data/orchestrator.db "PRAGMA index_list(agent_events);"
```

Ожидаются индексы:

- `idx_agent_events_created`
- `idx_agent_events_creative`
- `idx_agent_events_experiment`
