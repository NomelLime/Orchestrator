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
├── main_orchestrator.py          # Точка входа, главный цикл (9 шагов)
├── config.py                     # Все пути, пороги, интервалы
├── db/
│   ├── schema.sql                # SQLite схема (8 таблиц, вкл. proxy_events)
│   ├── connection.py             # init_db(), get_db() context manager
│   ├── zones.py                  # get_zone(), update_zone_score()
│   ├── experiences.py            # mark_plan_applied/failed, save_applied_change(),
│   │                             # update_metric_impact(), get_rich_experience_context()
│   ├── metrics.py                # save_metrics_snapshot()
│   └── commands.py               # get_policy(), is_zone_frozen()
├── modules/
│   ├── tracking.py               # Сбор метрик из SP + PL → БД
│   ├── evolution.py              # LLM-анализ → JSON план эволюции (бизнес-роль)
│   ├── evaluator.py              # 24h ретроспективная оценка applied_changes
│   ├── config_enforcer.py        # Применение config_changes (Zone 1, 2, 3)
│   ├── code_evolver.py           # Применение code_patches + check_and_revert_on_crash()
│   ├── supply_tracker.py         # Мониторинг прокси + Telegram-подтверждения
│   ├── zones.py                  # run_decay(), activate/deactivate логика
│   └── policies.py               # Обработка команд оператора → политики
├── commander/
│   ├── telegram_bot.py           # Polling, /proxies, да/нет подтверждения
│   └── notifier.py               # send_message(), log_notification(), дайджест
├── integrations/
│   ├── ollama_client.py          # Обёртка над Ollama API
│   ├── shorts_project.py         # analytics.json, agent_memory.json, crash detection
│   ├── prelend.py                # Чтение clicks.db, shave_report.json
│   ├── git_tools.py              # Автокоммит, find_last_orc_commit(), revert_commit()
│   └── proxy_manager.py          # mobileproxy.space API (баланс, прокси, покупка)
├── tests/
│   ├── conftest.py               # Фикстуры (make_sp_db, make_prelend_db, tmp_env)
│   ├── test_db_zones.py
│   ├── test_tracking.py
│   ├── test_evolution.py
│   └── test_zones_module.py
├── data/
│   ├── orchestrator.db           # SQLite БД (создаётся при первом запуске)
│   └── orchestrator.log          # Лог главного цикла
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
Шаг 0  — Ретроспективная оценка изменений (24h → metric_impact_json)
Шаг 1  — Деградация зон (passive decay)
Шаг 2  — Обработка pending команд оператора из Telegram
       → проверка pause_evolution политики
Шаг 3  — Сбор метрик из SP (analytics.json, agent_memory.json)
          и PL (clicks.db, shave_report.json) → metrics_snapshots
Шаг 3.5 — Краш-луп детектор: читает agent_memory.json events
          → если агент 3+ restart_requested за час →
            git revert последнего [Orchestrator/...] коммита в SP
          → при откате: пропускаем шаги 4–6 (цикл завершается)
Шаг 3.6 — Мониторинг прокси (раз в ORC_SUPPLY_CHECK_CYCLES циклов)
          → проверка баланса/истечения/бан-спайка
          → запрос оператору в Telegram: "да {id} / нет {id}"
Шаг 4  — LLM-анализ + генерация JSON плана → evolution_plans
       → отправка плана в Telegram
       [если DRY_RUN=true → выход]
Шаг 5  — Применение config_changes (Zone 1, 2, 3)
Шаг 6  — Применение code_patches (Zone 4, только .py / ShortsProject)
Шаг 7  — Суточный дайджест (если пришло время ORC_DIGEST_TIME)
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

---

## ИНТЕГРАЦИЯ С SP И PL

### Что Orchestrator читает из ShortsProject
- `data/analytics.json` — views, likes, CTR, A/B winner
- `data/agent_memory.json` — KV-хранилище агентов (в т.ч. `rec.strategist.*` — рекомендации STRATEGIST)

### Что Orchestrator читает из PreLend
- `data/clicks.db` — клики за период (total, CR, bot_pct, top GEO)
- `data/shave_report.json` — подозрения на шейв (shave_suspects)
- `data/agent_memory.json` — KV-хранилище PL агентов

### Что Orchestrator пишет
- SP: `pipeline/config.py` (Zone 1), A/B параметры (Zone 2), Python-патчи (Zone 4)
- PL: `config/settings.json` (Zone 3 thresholds), `config/advertisers.json` (Zone 3 advertiser_rate)
- Git autocommit после каждого изменения

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

# Краш-луп детектор
ORC_CRASH_WINDOW_MIN=60             # Окно анализа (минут)
ORC_CRASH_MIN_RESTARTS=3            # Минимум restart_requested для автооткатa
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
Фаза: MVP реализован, интеграция с SP и PL завершена. DRY_RUN на первом боевом запуске.

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
[ ] Этап 11 — Первый боевой запуск (DRY_RUN=true → проверка → DRY_RUN=false)
[ ] Этап 12 — Накопление опыта, снятие DRY_RUN, постепенное включение зон

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
