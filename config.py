"""
config.py — Центральный конфиг Orchestrator.

Все пути, интервалы и пороги задаются здесь.
Ничего не захардкожено в других модулях.
"""

from __future__ import annotations
import os
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Пути
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent

# Путь к базе данных Orchestrator
DB_PATH = BASE_DIR / "data" / "orchestrator.db"

# Пути к управляемым проектам
SHORTS_PROJECT_DIR = BASE_DIR.parent / "ShortsProject"
PRELEND_DIR        = BASE_DIR.parent / "PreLend"

# Конкретные файлы в ShortsProject, которые Orchestrator читает или пишет
SP_ANALYTICS_FILE  = SHORTS_PROJECT_DIR / "data" / "analytics.json"
SP_AGENT_MEMORY    = SHORTS_PROJECT_DIR / "data" / "agent_memory.json"
SP_CONFIG_PY       = SHORTS_PROJECT_DIR / "pipeline" / "config.py"
SP_ACCOUNTS_DIR    = SHORTS_PROJECT_DIR / "accounts"        # директория с аккаунтами
SP_LOG_FILE        = SHORTS_PROJECT_DIR / "data" / "pipeline.log"
SP_PYTEST_CMD      = ["python", "-m", "pytest", str(SHORTS_PROJECT_DIR / "tests"), "-q", "--tb=short"]

# Конкретные файлы в PreLend, которые Orchestrator читает или пишет
PL_SETTINGS        = PRELEND_DIR / "config" / "settings.json"
PL_ADVERTISERS     = PRELEND_DIR / "config" / "advertisers.json"
PL_AGENT_MEMORY    = PRELEND_DIR / "data" / "agent_memory.json"
PL_CLICKS_DB       = PRELEND_DIR / "data" / "clicks.db"
PL_SHAVE_REPORT    = PRELEND_DIR / "data" / "shave_report.json"
# NOTE: PHP тесты слабые — Zone 4 для PreLend заблокирована намеренно (см. zones.py)

# ─────────────────────────────────────────────────────────────────────────────
# Тайминги
# ─────────────────────────────────────────────────────────────────────────────

# Главный цикл Orchestrator
CYCLE_INTERVAL_HOURS = int(os.getenv("ORC_CYCLE_HOURS", "1"))

# Предотвращение перекрытия циклов: если предыдущий цикл ещё идёт — пропустить
# (реализуется через lock-файл в main_orchestrator.py)
CYCLE_LOCK_FILE = BASE_DIR / "data" / ".cycle.lock"

# Задержка перед применением плана после генерации (сек), позволяет
# прочитать план в Telegram до того, как он начнёт применяться
PLAN_APPLY_DELAY_SEC = 0   # TODO: увеличить если нужна ручная проверка

# ─────────────────────────────────────────────────────────────────────────────
# Пороги зон доверия (confidence_score, 0–100)
# ─────────────────────────────────────────────────────────────────────────────

# Zone активируется, если score >= ACTIVATE_THRESHOLD
ZONE_ACTIVATE_THRESHOLD = 70

# Zone деактивируется, если score < DEACTIVATE_THRESHOLD (hysteresis)
ZONE_DEACTIVATE_THRESHOLD = 30

# За каждый успешный применённый план — прибавляем к score
ZONE_SCORE_SUCCESS_DELTA = 5

# За каждый откат (тесты не прошли / метрики ухудшились) — отнимаем
ZONE_SCORE_FAILURE_DELTA = 20

# Пассивная деградация: если зона не применялась X дней — score снижается
ZONE_DECAY_DAYS          = 7    # период без применения → начинаем деградацию
ZONE_DECAY_PER_DAY       = 10   # на сколько снижается за каждый день

# ─────────────────────────────────────────────────────────────────────────────
# Ограничения Code Evolver (Zone 4)
# ─────────────────────────────────────────────────────────────────────────────

# PHP патчинг через LLM ненадёжен и тесты PreLend слабые.
# Zone 4 разрешена ТОЛЬКО для Python-файлов в ShortsProject.
CODE_EVOLVER_ALLOWED_EXTENSIONS = [".py"]
CODE_EVOLVER_ALLOWED_REPOS      = ["ShortsProject"]   # "PreLend" — запрещено
CODE_EVOLVER_MAX_PATCH_LINES    = 100   # не генерировать патчи > 100 строк за раз

# ─────────────────────────────────────────────────────────────────────────────
# LLM (Ollama)
# ─────────────────────────────────────────────────────────────────────────────

OLLAMA_HOST            = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_STRATEGY_MODEL  = os.getenv("ORC_STRATEGY_MODEL", "llama3.1")   # стратегический анализ
OLLAMA_CODE_MODEL      = os.getenv("ORC_CODE_MODEL",     "qwen2.5-coder:7b")  # Code Evolver

# Таймаут одного вызова LLM (сек). На RTX 3060 12GB Llama3.1 ≈ 30-90 сек.
OLLAMA_TIMEOUT_SEC     = int(os.getenv("ORC_LLM_TIMEOUT", "120"))

# ─────────────────────────────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN  = os.getenv("ORC_TG_TOKEN", "")   # токен бота Orchestrator
TELEGRAM_CHAT_ID    = os.getenv("ORC_TG_CHAT_ID", "")  # твой chat_id

# Время ежедневной сводки (локальное время, HH:MM)
DAILY_DIGEST_TIME   = os.getenv("ORC_DIGEST_TIME", "09:00")

# ─────────────────────────────────────────────────────────────────────────────
# Git
# ─────────────────────────────────────────────────────────────────────────────

# Автокоммит после каждого применённого изменения конфига / патча кода
GIT_AUTOCOMMIT = True
GIT_AUTHOR     = "Orchestrator <orchestrator@local>"

# ─────────────────────────────────────────────────────────────────────────────
# Proxy supply (mobileproxy.space)
# ─────────────────────────────────────────────────────────────────────────────

MOBILEPROXY_API_KEY      = os.getenv("ORC_MOBILEPROXY_API_KEY", "")

# Минимальный баланс в рублях — ниже него алерт в Telegram
PROXY_MIN_BALANCE_RUB    = float(os.getenv("ORC_PROXY_MIN_BALANCE", "300"))

# Предупреждение об истечении прокси за N дней
PROXY_EXPIRY_WARN_DAYS   = int(os.getenv("ORC_PROXY_EXPIRY_DAYS", "3"))

# Порог бан-событий за 24ч для рекомендации нового прокси
PROXY_BAN_SPIKE_THRESH   = int(os.getenv("ORC_PROXY_BAN_THRESH", "5"))

# Проверка прокси раз в N циклов (не на каждом цикле)
SUPPLY_CHECK_EVERY_N_CYCLES = int(os.getenv("ORC_SUPPLY_CHECK_CYCLES", "6"))

# ─────────────────────────────────────────────────────────────────────────────
# Краш-луп детектор (Phase D)
# ─────────────────────────────────────────────────────────────────────────────

# Окно анализа краш-лупа в минутах
CRASH_LOOP_WINDOW_MIN    = int(os.getenv("ORC_CRASH_WINDOW_MIN", "60"))

# Минимальное число restart_requested за окно для признания краш-лупа
CRASH_LOOP_MIN_RESTARTS  = int(os.getenv("ORC_CRASH_MIN_RESTARTS", "3"))

# ─────────────────────────────────────────────────────────────────────────────
# ShortsProject Pipeline Runner
# Orchestrator запускает run_pipeline.py как subprocess и управляет им
# ─────────────────────────────────────────────────────────────────────────────

# Включить автоматический запуск SP pipeline
SP_PIPELINE_ENABLED = os.getenv("ORC_SP_PIPELINE", "true").lower() == "true"

# Минимальный интервал между запусками pipeline (часы) — защита от спама
SP_PIPELINE_INTERVAL_HOURS = int(os.getenv("ORC_SP_INTERVAL_HOURS", "6"))

# Порог глубины upload_queue: если суммарно по всем аккаунтам меньше N видео → запуск
SP_PIPELINE_QUEUE_THRESHOLD = int(os.getenv("ORC_SP_QUEUE_THRESHOLD", "5"))

# Максимальное время выполнения pipeline (часы) — после этого считаем зависшим → алерт
SP_PIPELINE_MAX_DURATION_HOURS = int(os.getenv("ORC_SP_MAX_HOURS", "4"))

# PID-файл для хранения PID между перезапусками Orchestrator
SP_PIPELINE_PID_FILE = BASE_DIR / "data" / ".sp_pipeline.pid"

# ─────────────────────────────────────────────────────────────────────────────
# Режим безопасности
# ─────────────────────────────────────────────────────────────────────────────

# DRY_RUN=True → Orchestrator генерирует планы, но не применяет ничего.
# Используй при первом запуске, чтобы убедиться что логика корректна.
DRY_RUN = os.getenv("ORC_DRY_RUN", "false").lower() == "true"
