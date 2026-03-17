# Orchestrator — status.md
> Не пушить в гит. Выдавать в чате при старте каждой сессии.

---

## РОЛЬ
Главный агент-оркестратор над ShortsProject и PreLend.
Автономно собирает метрики, анализирует через LLM, генерирует и применяет планы эволюции.
Единственный получатель команд от оператора через Telegram.
SP и PL — гибкие исполнители; дублирование анализа исключено.


---

## СТЕК
| Слой         | Технология                                      |
|--------------|-------------------------------------------------|
| Рантайм      | Python 3.11+                                    |
| БД           | SQLite (data/orchestrator.db)                   |
| LLM          | Ollama (локально) — стратегический + code model |
| Уведомления  | Telegram Bot API (requests, без PTB)            |
| Блокировка   | portalocker (защита от перекрытия циклов)       |
| Git          | subprocess + git CLI (автокоммит изменений)     |

---

## СТРУКТУРА ПРОЕКТА
```
Orchestrator/
├── main_orchestrator.py          # Точка входа, главный цикл (8 шагов)
├── startup_check.py              # Проверка зависимостей при запуске (FAIL → exit)
├── config.py                     # Все пути, пороги, интервалы (+ SP pipeline params)
├── db/
│   ├── schema.sql                # SQLite схема (9 таблиц, вкл. proxy_events, pending_patches)
│   ├── connection.py             # init_db(), get_db() context manager
│   ├── zones.py                  # get_zone(), update_zone_score()
│   ├── experiences.py            # mark_plan_applied/failed, save_applied_change(),
│   │                             # update_metric_impact(), get_rich_experience_context()
│   ├── metrics.py                # save_metrics_snapshot()
│   ├── commands.py               # get_policy(), is_zone_frozen()
│   └── patches.py                # CRUD pending_patches (queue/approve/reject/get_approved)
├── modules/
│   ├── tracking.py               # Сбор метрик из SP + PL → БД
│   ├── evolution.py              # LLM-анализ → JSON план (ROI-фреймирование, justification)
│   ├── evaluator.py              # 24h ретроспективная оценка applied_changes
│   ├── config_enforcer.py        # Применение config_changes (Zone 1, 2, 3)
│   ├── code_evolver.py           # queue_code_patches() + apply_approved_patches() + crash revert
│   ├── supply_tracker.py         # Мониторинг прокси + Telegram-подтверждения
│   ├── sp_runner.py              # SP Pipeline manager: subprocess + PID + watchdog + TG notify
│   ├── zones.py                  # run_decay(), activate/deactivate логика
│   └── policies.py               # Обработка команд оператора → политики
├── commander/
│   ├── telegram_bot.py           # Polling, /proxies, /approve_N, /reject_N, /patches
│   └── notifier.py               # send_message(), log_notification(), дайджест
├── integrations/
│   ├── ollama_client.py          # Обёртка над Ollama API (+ shared GPU lock)
│   ├── shared_gpu_lock.py        # Кросс-процессный GPU lock (shared с SP через .gpu_lock)
│   ├── shorts_project.py         # analytics.json, agent_memory.json, crash detection
│   ├── prelend.py                # Обёртка обратной совместимости → prelend_client
│   ├── prelend_client.py         # HTTP-клиент к PreLend Internal API (NEW)
│   ├── git_tools.py              # Автокоммит, find_last_orc_commit(), revert_commit(), get_commit_timestamp()
│   └── proxy_manager.py          # mobileproxy.space API (баланс, прокси, покупка)
├── tests/
│   ├── conftest.py               # Фикстуры (make_sp_db, make_prelend_db, tmp_env)
│   ├── test_db_zones.py
│   ├── test_tracking.py
│   ├── test_evolution.py
│   └── test_zones_module.py
├── data/
│   ├── orchestrator.db           # SQLite БД (создаётся при первом запуске)
│   ├── orchestrator.log          # Лог главного цикла
│   └── .sp_pipeline.pid          # PID запущенного SP subprocess (persistence across restarts)
├── .env.example
└── requirements.txt
```

---

## 4 ЗОНЫ ВЛИЯНИЯ
| Зона | Имя        | Начальный score | Начальное состояние | Что меняет                                              |
|------|------------|-----------------|---------------------|--------------------------------------------------------|
| 1    | scheduling | 70              | enabled             | SP upload_schedule (время публикации, платформы)        |
| 2    | visual     | 50              | disabled            | SP A/B тесты: превью, хэштеги, заголовки               |
| 3    | prelend    | 30              | disabled            | PL settings.json (пороги алертов), advertisers.json (ставки) |
| 4    | code       | 20              | disabled            | Python-файлы ShortsProject (pytest guard, rollback)    |

Zone 4 заблокирована для PreLend навсегда: PHP патчинг через LLM ненадёжен, тесты слабые.

### Confidence Score
- +5 за каждый успешный применённый план
- −20 за откат (тесты не прошли / метрики ухудшились)
- Активируется при score ≥ 70, деактивируется при score < 30 (гистерезис)
- Пассивная деградация: если зона не применялась 7+ дней → −10/день

---

## ГЛАВНЫЙ ЦИКЛ (раз в CYCLE_INTERVAL_HOURS)
```
Шаг 0   — Ретроспективная оценка изменений (24h → metric_impact_json)
Шаг 1   — Деградация зон (passive decay)
Шаг 2   — Обработка pending команд оператора из Telegram
        → проверка pause_evolution политики
Шаг 3   — Сбор метрик из SP (analytics.json, agent_memory.json)
           и PL (clicks.db, shave_report.json) → metrics_snapshots
Шаг 3.5 — Краш-луп детектор: читает agent_memory.json events
           → если агент 3+ restart_requested за час →
             git revert последнего [Orchestrator/...] коммита в SP
           → при откате: пропускаем шаги 4–8 (цикл завершается)
Шаг 3.6 — SP Pipeline manager: запускает run_pipeline.py как subprocess
           если: не запущен + интервал выдержан + очередь < порога
           Watchdog: если зависает > SP_PIPELINE_MAX_HOURS → TG алерт
Шаг 3.7 — Мониторинг прокси (раз в ORC_SUPPLY_CHECK_CYCLES циклов)
           → проверка баланса/истечения/бан-спайка
           → запрос оператору в Telegram: "да {id} / нет {id}"
Шаг 4   — LLM-анализ + генерация JSON плана → evolution_plans
        → отправка плана в Telegram
        [если DRY_RUN=true → выход]
Шаг 5   — Применение config_changes (Zone 1, 2, 3)
Шаг 6a  — Применение ранее одобренных code_patches (из БД, статус approved)
Шаг 6b  — Постановка новых code_patches в очередь → diff в Telegram → ожидание /approve_N
Шаг 7   — Суточный дайджест (если пришло время ORC_DIGEST_TIME)
```

---

## ТАБЛИЦЫ SQLite
| Таблица              | Назначение                                              |
|----------------------|---------------------------------------------------------|
| `zones`              | Состояние 4 зон (enabled, confidence_score)             |
| `evolution_plans`    | Сгенерированные планы (JSON, статус, риск)              |
| `applied_changes`    | Каждое применённое изменение + metric_impact_json (24h) |
| `operator_commands`  | Команды оператора из Telegram                           |
| `operator_policies`  | Активные политики (freeze_zone, pause_evolution и т.д.) |
| `metrics_snapshots`  | Снапшоты метрик SP и PL каждый цикл                     |
| `notifications`      | Буфер событий для суточного дайджеста                   |
| `proxy_events`       | Запросы на покупку/продление прокси (да/нет flow)       |
| `pending_patches`    | Code patches ожидающие Telegram-одобрения (/approve_N)  |

---

## ИНТЕГРАЦИЯ С SP И PL

### Что Orchestrator читает из ShortsProject
- `data/analytics.json` — views, likes, CTR, A/B winner
- `data/agent_memory.json` — KV-хранилище агентов (в т.ч. `rec.strategist.*` — рекомендации STRATEGIST)

### Что Orchestrator читает/пишет в PreLend
PreLend находится на VPS. Доступ — через PreLend Internal API (HTTP, порт 9090).
Туннель: `ssh -N -L 9090:127.0.0.1:9090 user@vps-ip`

| Операция | Метод | Эндпоинт |
|----------|-------|----------|
| Метрики (клики, CR, бот%) | GET | `/metrics?period_hours=24` |
| Конверсии с payout | GET | `/metrics/financial` |
| Воронка SP→PL | GET | `/metrics/funnel` |
| Чтение settings.json | GET | `/config/settings` |
| Запись settings.json (Zone 3) | PUT | `/config/settings` |
| Чтение/запись advertisers.json | GET/PUT | `/config/advertisers` |

Git commit при записи выполняется на стороне VPS (внутри Internal API).

### Избегание дублирования
| Что могло дублироваться          | Решение                                                   |
|----------------------------------|-----------------------------------------------------------|
| STRATEGIST (SP) + Orchestrator LLM | Orchestrator читает `rec.strategist.*` из KV, не вызывает своё GPU |
| Дайджест PL + Orchestrator       | `PL_DISABLE_DAILY_DIGEST=true` — дайджест только у Orchestrator |
| Polling SP + PL + Orchestrator   | `SP/PL_DISABLE_TELEGRAM_POLLING=true` — команды только через Orchestrator |
| Уведомления SP/PL → Telegram     | `SP/PL_TELEGRAM_CRITICAL_ONLY=true` — только критические alert() |

---

## ENV-ПЕРЕМЕННЫЕ (.env)
```env
# Telegram
ORC_TG_TOKEN=your_bot_token_here    # Единственный бот в чате
ORC_TG_CHAT_ID=your_chat_id_here

# LLM (Ollama)
ORC_STRATEGY_MODEL=llama3.1         # Анализ метрик и генерация планов
ORC_CODE_MODEL=qwen2.5-coder:7b     # Генерация code_patches (Zone 4)
ORC_LLM_TIMEOUT=120                 # Таймаут вызова (сек)

# Цикл
ORC_CYCLE_HOURS=1                   # Интервал главного цикла
ORC_DIGEST_TIME=09:00               # Время суточной сводки (local time)

# Безопасность
ORC_DRY_RUN=true                    # При первом запуске — true!
                                    # Планы генерируются, но не применяются

# Прокси (mobileproxy.space)
ORC_MOBILEPROXY_API_KEY=            # API-ключ из личного кабинета
ORC_PROXY_MIN_BALANCE=300           # Порог баланса для уведомления (руб.)
ORC_PROXY_EXPIRY_DAYS=3             # За N дней до истечения → запрос
ORC_PROXY_BAN_THRESH=5              # N банов за 24ч → рекомендация докупить
ORC_SUPPLY_CHECK_CYCLES=6           # Проверять раз в N циклов

# Задержка перед применением плана (сек). 0 = отключено (для тестов/DRY_RUN)
ORC_PLAN_APPLY_DELAY=300           # 5 минут — оператор может отправить /freeze

# PreLend Internal API
PL_INTERNAL_API_URL=http://localhost:9090  # SSH tunnel: ssh -N -L 9090:127.0.0.1:9090 user@vps
PL_INTERNAL_API_KEY=your-secret-key-here

# SP Pipeline manager (шаг 3.6)
ORC_SP_PIPELINE=true                # Включить авто-запуск run_pipeline.py
ORC_SP_INTERVAL_HOURS=6             # Минимальный интервал между запусками
ORC_SP_QUEUE_THRESHOLD=5            # Запуск только если очередь < N видео
ORC_SP_MAX_HOURS=4                  # Алерт если SP висит дольше N часов
```

---

## ЗАПУСК
```bash
# Первый запуск (безопасный)
ORC_DRY_RUN=true python main_orchestrator.py

# Production
python main_orchestrator.py

# Тесты
python -m pytest tests/ -v
```

Orchestrator пишет лог в `data/orchestrator.log` и в stdout.

---

## ZONE 3 — РАЗРЕШЁННЫЕ ПАРАМЕТРЫ
Orchestrator может менять только 5 порогов в PreLend `settings.json`:
```
bot_pct_per_hour        — порог алерта на % ботов в час
offgeo_pct_per_hour     — порог алерта на нецелевой трафик
shave_threshold_pct     — порог подозрения на шейв
landing_slow_ms         — порог медленного лендинга (мс)
landing_down_alert_min  — минут недоступности до алерта
```
И ставку рекламодателя (`scope=advertiser_rate`) в `advertisers.json`.
Все остальные ключи — запрещены (whitelist в `config_enforcer.py`).

---

## СТАТУС РАЗРАБОТКИ
Фаза: MVP + расширенная аналитика + self-healing. DRY_RUN на первом боевом запуске.

[x] Этап 1 — Архитектура, схема БД, config.py
[x] Этап 2 — db/ слой (connection, zones, experiences, metrics, commands)
[x] Этап 3 — modules/tracking.py (сбор метрик из SP + PL)
[x] Этап 4 — modules/evolution.py (LLM-генерация планов)
[x] Этап 5 — modules/config_enforcer.py (Zone 1, 2, 3 применение)
[x] Этап 6 — modules/code_evolver.py (Zone 4, pytest guard, rollback)
[x] Этап 7 — commander/ (Telegram polling, notifier, суточный дайджест)
[x] Этап 8 — integrations/ (ollama_client, shorts_project, prelend, git_tools)
[x] Этап 9 — Исключение дублирования (STRATEGIST, дайджест, polling)
    (SP + PL отключают своё polling и дайджест, Orchestrator — единственный командный центр)
[x] Этап 10 — tests/ (conftest + 4 test-модуля)
[x] Этап 11 — Единая точка входа: startup_check + sp_runner + code patch approval flow
[x] Этап 12 (plan) — FinancialObserver: ROI tracking + инжекция в LLM
    (`modules/financial_observer.py`, `db/finances.py`, `financial_records` таблица, evolution.py ROI block)
[x] Этап 13 (plan) — Расписание по часовым поясам
    (`modules/timezone_mapper.py`, config_enforcer.py UTC conversion для Zone 1)
[x] Этап 14 (plan) — Кросс-проектная аналитика (воронка SP → PreLend)
    (`modules/funnel_linker.py`, `funnel_events` таблица, analytics.py `prelend_sub_id`)
[x] Этап 15 (plan) — Self-healing (откат конфига агента при краш-лупе)
    (`modules/agent_healer.py`, `agent_config_snapshots` таблица, config_enforcer.py snapshot)
[ ] Боевой запуск — DRY_RUN=true → проверка → DRY_RUN=false → накопление опыта
[ ] Настроить SSH tunnel / WireGuard для доступа к PreLend Internal API на VPS

---

## ИСТОРИЯ СЕССИЙ

### Сессия 1 — Инициализация проекта
Создана полная структура: db/, modules/, commander/, integrations/, tests/.
Schema.sql с 7 таблицами. config.py. main_orchestrator.py с 7-шаговым циклом.
Zone 1 (scheduling) сразу включена (score=70), остальные отключены.

### Сессия 2 — Исправление conftest.py
Фикстура `make_prelend_db` имела INSERT только с 6 из 16 колонок clicks.
Добавлена таблица `advertiser_rates`, 4 индекса, полный 16-колоночный INSERT.

### Сессия 3 (11.03.2026) — Исключение дублирования, Zone 3
| Файл | Что изменилось |
|------|---------------|
| `modules/tracking.py` | Читает `rec.strategist.*` из SP agent_memory KV |
| `modules/evolution.py` | Инжектирует рекомендации STRATEGIST в LLM-промпт; Zone 3 примеры |
| `modules/config_enforcer.py` | Zone 3 реализована: `_apply_pl_thresholds()` + `_apply_pl_advertiser_rate()` |
| `commander/notifier.py` | Дайджест включает метрики SP + PL из metrics_snapshots; zone icons 🔒 |
| `.env.example` | Обновлён (секции с комментарием о едином боте) |

### Сессия 4 (11.03.2026) — Autonomous Executive Level
Апгрейд до автономного управления: память опыта, патчинг кода, прокси и краш-детектор.

**Phase A — Memory Loop (24h деferred evaluation)**
| Файл | Что изменилось |
|------|---------------|
| `modules/evaluator.py` (NEW) | 24h ретроспективная оценка applied_changes → заполняет metric_impact_json |
| `db/experiences.py` | + `update_metric_impact()`, `get_rich_experience_context()` |
| `modules/evolution.py` | Промпт перефреймирован как «владелец бизнеса»; показывает реальные дельты опыта (views +12%, CR -3%) |

**Phase C — ProxyManager (mobileproxy.space)**
| Файл | Что изменилось |
|------|---------------|
| `integrations/proxy_manager.py` (NEW) | API-обёртка: баланс, прокси, покупка, продление, ротация IP |
| `modules/supply_tracker.py` (NEW) | Мониторинг: баланс/истечение/бан-спайк → Telegram-запрос «да N / нет N» |
| `db/schema.sql` | + таблица `proxy_events` (8-я таблица) |
| `commander/telegram_bot.py` | + `/proxies` команда; `_handle_text` обрабатывает «да/нет» подтверждения |
| `config.py` | + proxy supply и crash loop переменные |

**Phase D — Crash Loop Detector + Auto Revert**
| Файл | Что изменилось |
|------|---------------|
| `integrations/shorts_project.py` | + `get_crash_loop_agents()` — читает events из agent_memory.json |
| `integrations/git_tools.py` | + `find_last_orc_commit()`, `revert_commit()` |
| `modules/code_evolver.py` | + `check_and_revert_on_crash()` — автооткат при 3+ restart за 1ч |
| `main_orchestrator.py` | + Шаги 0, 3.5, 3.6; cycle_num для throttle |

**Дорожная карта (отложено)**
- FinancialObserver + ROI tracking — реализовать когда постбэки PreLend заработают

### Сессия 7 (15.03.2026) — ContentHub интеграция + 4 новых модуля

Реализованы Этапы 5, 11–13 плана 15 фич.

**FinancialObserver (Этап 5 план):**

| Файл | Суть |
|------|------|
| `db/finances.py` (NEW) | CRUD `financial_records`: `add_record()`, `record_exists(external_id)` (dedup), `get_summary(days)` → net/roi/by_source/by_day, `get_recent_records()` |
| `modules/financial_observer.py` (NEW) | `collect_all()`: PreLend конверсии (payout из notes, dedup `pl_conv_{id}`), SP монетизация (dedup `sp_mon_{stem}_{platform}`), прокси из proxy_events (dedup `proxy_evt_{id}`) |
| | `get_financial_context(days)` → dict для LLM: `net_roi_rub`, `revenue_rub`, `expense_rub`, `roi_pct`, `net_roi_7d_rub`, `roi_7d_pct`, `by_source` |
| `modules/evolution.py` | + `finances_block` перед strategist_block в LLM-промпте (реальные рубли, не абстрактный ROI) |
| `db/schema.sql` | + таблица `financial_records` (id, recorded_at, category, source, amount_rub, description, period_start, period_end, external_id, auto_collected) |

**Расписание по часовым поясам (Этап 11 план):**

| Файл | Суть |
|------|------|
| `modules/timezone_mapper.py` (NEW) | 80+ стран: `geo_utc_offset(geo)`, `local_to_utc(time, geo)`, `utc_to_local(time, geo)`, `convert_schedule(times, geo)` — конвертация + сортировка + дедупликация |
| `modules/config_enforcer.py` | `_apply_sp_schedule()`: если `target_geo` в change → `convert_schedule()` перед записью UTC-времён |

**Кросс-проектная аналитика (Этап 12 план):**

| Файл | Суть |
|------|------|
| `modules/funnel_linker.py` (NEW) | `link_funnel()`: JOIN SP `analytics.json` (по `sp_{stem}`) с PreLend `clicks.db` (`utm_content = prelend_sub_id`) → upsert в `funnel_events` (`ON CONFLICT DO UPDATE`) |
| | PreLend DB открывается read-only (`uri=True, mode=ro`); `get_funnel_data(limit)` для ContentHub dashboard |
| `db/schema.sql` | + таблица `funnel_events` (sp_stem, platform, video_url, prelend_sub_id, views, clicks, conversions, revenue_rub) |
| `ShortsProject/pipeline/analytics.py` | `register_upload()` добавляет `"prelend_sub_id": f"sp_{stem}"` в каждый upload |

**Self-healing (Этап 13 план):**

| Файл | Суть |
|------|------|
| `modules/agent_healer.py` (NEW) | `snapshot_config(agent_name, config_file, plan_id)` → INSERT в `agent_config_snapshots` |
| | `check_and_heal(agent_name, window_min, min_crashes)`: считает ERROR события из agent_memory.json 3 проектов, откат при краш-лупе |
| | `restore_snapshot(snapshot_id)` — ручной откат из ContentHub UI |
| | `_restore(snap)`: атомичная запись `config_json` → config_file (write-temp → os.replace) |
| | `_recently_healed()`: анти-цикл — проверяет notifications за последние 30 мин |
| `modules/config_enforcer.py` | `snapshot_config()` вызывается перед каждым применением изменения конфига |
| `db/schema.sql` | + таблица `agent_config_snapshots` (agent_name, config_file, config_json, applied_plan_id) |

---

### Сессия 6 (14.03.2026) — Полный code review + исправления (3 проекта)

Полный ревью всех трёх проектов (код, логика, безопасность, архитектура). Большинство критических проблем оказались уже исправленными в предыдущих сессиях.

**Уже было исправлено ранее (верифицировано):**
- `telegram_bot.py` — авторизация `_is_authorized()` во всех хендлерах
- `code_evolver.py` — path traversal (resolve + relative_to), unified_diff, backup в finally
- `evolution.py` / `policies.py` — string-aware balanced brace JSON parser
- `zones.py` — decay раз в день (`_last_decay_date`)
- `notifier.py` — дайджест по часу (не по точной минуте)
- `sp_runner.py` — log_file в try/finally
- `main_orchestrator.py` — lock file в AlreadyLocked handler
- `tracking.py` — `ban_count` через `startswith("ban_")`

**Исправлено в этой сессии:**

| Файл | Проблема | Исправление |
|------|----------|-------------|
| `db/zones.py` | SQL интерполяция через `.format()` | Два отдельных запроса + `is_zone_frozen()` проверка |
| `db/commands.py` | Нет очистки истекших политик | + `cleanup_expired_policies()` |
| `db/patches.py` | Нет лимита pending patches | + `MAX_PENDING_PATCHES = 20`, возврат -1 при лимите |
| `modules/code_evolver.py` | Не обрабатывал отрицательный patch_id | + early return False |
| `integrations/shared_gpu_lock.py` | Хардкод пути к `.gpu_lock` | `_cfg.BASE_DIR.parent / ".gpu_lock"` |
| `main_orchestrator.py` | Не вызывал очистку политик | + `cleanup_expired_policies()` в цикле |

### Сессия 8 (18.03.2026) — PreLend Internal API + рефакторинг доступа к PL

PreLend перенесён на VPS — прямой доступ к его файлам с локальной машины невозможен.
Реализован Internal API на VPS + HTTP-клиент, который используют оба проекта.

**Новые файлы:**

| Файл | Суть |
|------|------|
| `integrations/prelend_client.py` (NEW) | HTTP-клиент к PreLend Internal API. Singleton `get_client()`. Методы: `get_metrics()`, `get_financial_metrics()`, `get_funnel_data()`, `get/write_config()`, `get/write_settings/advertisers/geo_data/splits()`, `get_agents()`, `stop/start_agent()`, `is_available()` |

**Рефакторинг (замена файловых операций на HTTP):**

| Файл | Что изменилось |
|------|----------------|
| `integrations/prelend.py` | Переписан как тонкая обёртка над `prelend_client` (обратная совместимость) |
| `modules/tracking.py` | `collect_prelend_snapshot()` → `client.get_metrics()`. Graceful fallback при недоступности API. Удалены `sqlite3`, `time` |
| `modules/config_enforcer.py` | `_apply_pl_thresholds()` и `_apply_pl_advertiser_rate()` → `client.get/write_settings/advertisers()`. Git commit теперь на стороне VPS |
| `modules/financial_observer.py` | `_collect_prelend_revenue()` → `client.get_financial_metrics()`. Удалён прямой `sqlite3` коннект к clicks.db |
| `modules/funnel_linker.py` | `link_funnel()` → `client.get_funnel_data()`. Новые хелперы `_build_funnel_rows_from_api()`, `_calc_revenue_from_notes()`. Удалены `_open_prelend_db()`, `sqlite3` |
| `startup_check.py` | + `_check_prelend_api()` — WARN если API недоступен (не критично, не останавливает запуск) |
| `config.py` | + `PL_INTERNAL_API_URL`, `PL_INTERNAL_API_KEY` |
| `.env.example` | + секция PreLend Internal API с комментарием про SSH tunnel |
| `tests/conftest.py` | + `mock_prelend_client` autouse fixture — все тесты используют MagicMock, VPS не нужен |

**Также исправлено (сессия 8, часть 1):**

| Файл | Проблема | Исправление |
|------|----------|-------------|
| `config.py` | Хардкод `C:\Users\lemon\...` в `GITHUB_ROOT` | `EnvironmentError` если `GITHUB_ROOT` не задан в `.env` |
| `services/auth.py` | `import json as _json` внутри функции | Перенесён на верхний уровень |
| `integrations/git_tools.py` | Не было функции для получения timestamp коммита | + `get_commit_timestamp(repo_dir, commit_hash)` через `git log --format=%ct` |
| `modules/code_evolver.py` | Crash-loop revert откатывал коммиты вне временного окна | Проверка `commit_ts >= window_start` перед откатом |
| `config.py` | `PLAN_APPLY_DELAY_SEC = 0` (нет паузы перед применением) | Изменён на `300` (5 мин), привязан к `ORC_PLAN_APPLY_DELAY` |
| `main_orchestrator.py` | Нет уведомления перед sleep | + Telegram «⏳ Применение через N мин. /freeze» |
| `modules/code_evolver.py` | Нет санитизации `goal` и `file_name` перед LLM | + `_sanitize_for_prompt(value, max_len)` — удаляет управляющие символы, обрезает |

**Единая точка входа:**
- Теперь запускается только `python main_orchestrator.py`. SP pipeline стартует автоматически.

**startup_check.py (NEW):**
- Вызывается из `main()` до старта цикла (`abort_on_fail=True`)
- Проверяет: Python пакеты SP + ORC, ffmpeg/yt-dlp/playwright, Ollama + модели, env-переменные, пути
- FAIL → `sys.exit(1)`, WARN → предупреждение, продолжение
- Можно запустить отдельно: `python startup_check.py`

**modules/sp_runner.py (NEW):**
- `manage_sp_pipeline(metrics_data)` — шаг 3.6 главного цикла
- Условие запуска: не запущен + интервал ≥ `SP_PIPELINE_INTERVAL_HOURS` + очередь < `SP_PIPELINE_QUEUE_THRESHOLD`
- `subprocess.Popen` с PID-файлом `.sp_pipeline.pid` для сохранения состояния между перезапусками Orchestrator
- Watchdog: если висит > `SP_PIPELINE_MAX_DURATION_HOURS` → TG алерт

**Code Patch Telegram Approval Flow:**

| Файл | Что изменилось |
|------|---------------|
| `db/schema.sql` | + таблица `pending_patches` (9-я таблица) |
| `db/patches.py` (NEW) | CRUD: `queue_patch()`, `approve_patch()`, `reject_patch()`, `get_approved_patches()` |
| `modules/code_evolver.py` | Шаг 6b: `queue_code_patches()` → diff → Telegram, без авто-применения. Шаг 6a: `apply_approved_patches()` → применяет только одобренные |
| `commander/telegram_bot.py` | + `/approve_N`, `/reject_N`, `/patches` handlers |

**integrations/shared_gpu_lock.py (NEW):**
- Кросс-процессная блокировка Ollama — shared файл `../../.gpu_lock` с SP

**modules/evolution.py:**
- ROI-фреймирование промпта: формула `views_delta_pct × engagement_rate × survival_rate × account_health`
- Поле `justification` в JSON-схеме code_patches
