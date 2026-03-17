"""
startup_check.py — Проверка всех зависимостей при запуске Orchestrator.

Вызывается автоматически из main_orchestrator.py перед стартом цикла.
Также можно запустить вручную: python startup_check.py

Уровни:
  FAIL  — критическая зависимость отсутствует → запуск прерывается
  WARN  — некритично, запуск продолжается, но функциональность ограничена
  OK    — всё в порядке

Что проверяется:
  1. Python-пакеты ShortsProject (из SP/requirements.txt)
  2. Python-пакеты Orchestrator (из ORC/requirements.txt)
  3. Внешние инструменты: ffmpeg, yt-dlp
  4. Ollama: сервер доступен + нужные модели загружены
  5. Переменные окружения: Telegram-токены обоих проектов
  6. Пути: ShortsProject dir, run_pipeline.py
"""

from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

import config

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Цвета (ANSI, работают на Windows 10+ с chcp 65001)
# ─────────────────────────────────────────────────────────────────────────────
G   = "\033[92m"
Y   = "\033[93m"
R   = "\033[91m"
W   = "\033[97m"
DIM = "\033[2m"
RST = "\033[0m"
SEP = f"{DIM}{'─' * 60}{RST}"


def _ok(msg: str)   -> None: print(f"  {G}✔{RST}  {msg}")
def _warn(msg: str) -> None: print(f"  {Y}⚠{RST}  {msg}")
def _fail(msg: str) -> None: print(f"  {R}✘{RST}  {W}{msg}{RST}")
def _head(title: str) -> None: print(f"\n{SEP}\n  {W}{title}{RST}\n{SEP}")


# ─────────────────────────────────────────────────────────────────────────────
# Мап "имя пакета в pip" → "import name"
# ─────────────────────────────────────────────────────────────────────────────
_IMPORT_MAP = {
    "ffmpeg-python":          "ffmpeg",
    "opencv-python":          "cv2",
    "Pillow":                 "PIL",
    "pillow":                 "PIL",
    "yt-dlp":                 "yt_dlp",
    "rebrowser-playwright":   "rebrowser_playwright",
    "playwright-stealth":     "playwright_stealth",
    "python-dotenv":          "dotenv",
    "python-telegram-bot":    "telegram",
    "kokoro-onnx":            "kokoro",
    "soundfile":              "soundfile",
    "langdetect":             "langdetect",
    "imagehash":              "imagehash",
    "numpy":                  "numpy",
    "requests":               "requests",
    "portalocker":            "portalocker",
    "tqdm":                   "tqdm",
    "psutil":                 "psutil",
    "streamlit":              "streamlit",
    "ollama":                 "ollama",
}

# Пакеты, отсутствие которых — только предупреждение (не критично)
_OPTIONAL_PACKAGES = {"streamlit", "kokoro-onnx", "kokoro", "soundfile", "langdetect"}


def _pkg_available(pip_name: str) -> bool:
    import_name = _IMPORT_MAP.get(pip_name, pip_name.replace("-", "_").lower())
    return importlib.util.find_spec(import_name) is not None


# ─────────────────────────────────────────────────────────────────────────────
# 1. Python-пакеты
# ─────────────────────────────────────────────────────────────────────────────

def _parse_requirements(path: Path) -> List[str]:
    """Читает requirements.txt и возвращает список имён пакетов."""
    packages = []
    if not path.exists():
        return packages
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        # Убираем версию: "requests>=2.31" → "requests"
        name = line.split(">=")[0].split("<=")[0].split("==")[0].split("~=")[0].strip()
        if name:
            packages.append(name)
    return packages


def check_python_packages(label: str, req_file: Path) -> int:
    """Проверяет наличие пакетов из requirements.txt. Возвращает число FAIL."""
    _head(f"Python-пакеты — {label}")
    packages = _parse_requirements(req_file)
    if not packages:
        _warn(f"requirements.txt не найден: {req_file}")
        return 0

    fails = 0
    for pkg in packages:
        if _pkg_available(pkg):
            import_name = _IMPORT_MAP.get(pkg, pkg.replace("-", "_").lower())
            _ok(f"{pkg} ({import_name})")
        elif pkg in _OPTIONAL_PACKAGES or pkg.lower() in _OPTIONAL_PACKAGES:
            _warn(f"{pkg}  {DIM}— не установлен (опционально){RST}")
        else:
            _fail(f"{pkg}  — НЕ УСТАНОВЛЕН  →  pip install {pkg}")
            fails += 1
    return fails


# ─────────────────────────────────────────────────────────────────────────────
# 2. Внешние инструменты
# ─────────────────────────────────────────────────────────────────────────────

def check_external_tools() -> int:
    _head("Внешние инструменты")
    fails = 0

    # ffmpeg
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        try:
            r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
            ver = r.stdout.decode(errors="replace").splitlines()[0].split()[2]
            _ok(f"ffmpeg {ver}  ({ffmpeg_path})")
        except Exception:
            _ok(f"ffmpeg найден: {ffmpeg_path}")
    else:
        _fail("ffmpeg не найден в PATH — обработка видео невозможна")
        fails += 1

    # yt-dlp
    ytdlp_path = shutil.which("yt-dlp") or shutil.which("ytdlp")
    if ytdlp_path:
        try:
            r = subprocess.run(["yt-dlp", "--version"], capture_output=True, timeout=5)
            ver = r.stdout.decode().strip()
            _ok(f"yt-dlp {ver}")
        except Exception:
            _ok(f"yt-dlp найден: {ytdlp_path}")
    elif _pkg_available("yt_dlp"):
        _ok("yt-dlp (Python-пакет, без системного бинарника)")
    else:
        _warn("yt-dlp не найден — скачивание видео недоступно")

    # Playwright Chromium
    try:
        from rebrowser_playwright.sync_api import sync_playwright
        _ok("rebrowser-playwright импортируется корректно")
    except ImportError:
        _fail("rebrowser-playwright не импортируется")
        fails += 1

    return fails


# ─────────────────────────────────────────────────────────────────────────────
# 3. Ollama
# ─────────────────────────────────────────────────────────────────────────────

def check_ollama() -> int:
    _head("Ollama (LLM-сервер)")

    import requests as _req

    # Доступность сервера
    try:
        resp = _req.get(f"{config.OLLAMA_HOST}/api/tags", timeout=5)
        if resp.status_code != 200:
            raise ValueError(f"HTTP {resp.status_code}")
        tags_data = resp.json()
        _ok(f"Ollama сервер доступен: {config.OLLAMA_HOST}")
    except Exception as e:
        _warn(f"Ollama недоступен: {e}  — AI-функции отключены до запуска сервера")
        return 0  # не критично — сервер может запуститься позже

    # Проверяем нужные модели
    loaded_models = {m["name"] for m in tags_data.get("models", [])}

    required_models = [
        (config.OLLAMA_STRATEGY_MODEL, "Orchestrator — стратегический анализ", True),
        (config.OLLAMA_CODE_MODEL,     "Orchestrator — Code Evolver",          True),
    ]

    # Модели ShortsProject читаем из его конфига
    sp_config_py = config.SHORTS_PROJECT_DIR / "pipeline" / "config.py"
    sp_vl_model = "qwen2.5-vl:7b"  # дефолт
    if sp_config_py.exists():
        for line in sp_config_py.read_text(encoding="utf-8").splitlines():
            if "OLLAMA_MODEL" in line and "=" in line and "#" not in line.split("=")[0]:
                val = line.split("=", 1)[1].strip().strip("'\"")
                if val:
                    sp_vl_model = val
                break
    required_models.append((sp_vl_model, "ShortsProject — VL анализ", True))

    for model_name, label, critical in required_models:
        # Проверяем точное совпадение и совпадение без тэга версии
        found = model_name in loaded_models or any(
            m.startswith(model_name.split(":")[0]) for m in loaded_models
        )
        if found:
            _ok(f"{model_name}  {DIM}({label}){RST}")
        elif critical:
            _warn(f"{model_name}  — не загружена  →  ollama pull {model_name}")
        else:
            _warn(f"{model_name}  — не найдена  {DIM}({label}){RST}")

    return 0


# ─────────────────────────────────────────────────────────────────────────────
# 4. Переменные окружения
# ─────────────────────────────────────────────────────────────────────────────

def check_env() -> int:
    _head("Переменные окружения")

    # Orchestrator Telegram
    orc_token   = config.TELEGRAM_BOT_TOKEN
    orc_chat_id = config.TELEGRAM_CHAT_ID
    if orc_token and orc_chat_id:
        _ok(f"ORC_TG_TOKEN / ORC_TG_CHAT_ID настроены")
    else:
        _warn("ORC_TG_TOKEN / ORC_TG_CHAT_ID не заданы — Telegram Orchestrator отключён")

    # ShortsProject Telegram (читаем из .env или SP config)
    try:
        sp_env = config.SHORTS_PROJECT_DIR / ".env"
        sp_tg_ok = False
        if sp_env.exists():
            for line in sp_env.read_text(encoding="utf-8").splitlines():
                if "TELEGRAM_BOT_TOKEN" in line and "=" in line:
                    val = line.split("=", 1)[1].strip().strip('"\'')
                    if val:
                        sp_tg_ok = True
                        break
        if sp_tg_ok:
            _ok("SP TELEGRAM_BOT_TOKEN настроен")
        else:
            _warn("SP TELEGRAM_BOT_TOKEN не найден в ShortsProject/.env")
    except Exception:
        _warn("Не удалось проверить SP Telegram config")

    # DRY_RUN предупреждение
    if config.DRY_RUN:
        _warn("DRY_RUN=true — изменения конфига и код-патчи НЕ применяются")
    else:
        _ok("DRY_RUN=false — боевой режим")

    return 0


# ─────────────────────────────────────────────────────────────────────────────
# 5. Пути к проектам
# ─────────────────────────────────────────────────────────────────────────────

def _check_prelend_api() -> int:
    """Проверяет доступность PreLend Internal API через HTTP."""
    _head("PreLend Internal API")

    api_url = config.PL_INTERNAL_API_URL
    api_key = config.PL_INTERNAL_API_KEY

    if not api_key:
        _warn(
            f"PL_INTERNAL_API_KEY не задан — API без аутентификации (dev-режим). "
            f"Задайте ключ в .env для продакшна."
        )

    try:
        from integrations.prelend_client import get_client
        client = get_client()
        if client.is_available():
            _ok(f"PreLend Internal API доступен: {api_url}")
        else:
            _warn(
                f"PreLend Internal API недоступен ({api_url}). "
                f"Zone 3 (PL конфиги), финансовый observer, воронка SP→PL не будут работать. "
                f"Запустите SSH tunnel: ssh -N -L 9090:127.0.0.1:9090 user@vps-ip"
            )
    except Exception as exc:
        _warn(f"Не удалось проверить PreLend API: {exc}")

    return 0  # недоступность API — не критично (Orchestrator продолжит работу)


def check_paths() -> int:    _head("Пути к проектам")
    fails = 0

    sp_dir = config.SHORTS_PROJECT_DIR
    if sp_dir.exists():
        _ok(f"ShortsProject: {sp_dir}")
    else:
        _fail(f"ShortsProject не найден: {sp_dir}")
        fails += 1
        return fails  # дальше нет смысла проверять SP-файлы

    run_pipeline = sp_dir / "run_pipeline.py"
    if run_pipeline.exists():
        _ok(f"run_pipeline.py найден")
    else:
        _fail(f"run_pipeline.py не найден в {sp_dir}")
        fails += 1

    accounts_dir = config.SP_ACCOUNTS_DIR
    if accounts_dir.exists():
        acc_count = sum(1 for p in accounts_dir.iterdir() if p.is_dir())
        if acc_count > 0:
            _ok(f"Аккаунтов найдено: {acc_count}")
        else:
            _warn(f"Директория аккаунтов пуста: {accounts_dir}")
    else:
        _warn(f"Директория аккаунтов не найдена: {accounts_dir}")

    pl_dir = config.PRELEND_DIR
    if pl_dir.exists():
        _ok(f"PreLend: {pl_dir}")
    else:
        _warn(f"PreLend не найден: {pl_dir}  {DIM}(некритично если не используется){RST}")

    return fails


# ─────────────────────────────────────────────────────────────────────────────
# Главная функция
# ─────────────────────────────────────────────────────────────────────────────

def run_checks(abort_on_fail: bool = True) -> bool:
    """
    Запускает все проверки и выводит отчёт.

    Args:
        abort_on_fail: если True (по умолчанию) и есть FAIL — вызывает sys.exit(1)

    Returns:
        True если всё ок или только предупреждения, False если есть FAIL.
    """
    print(f"\n{'═' * 60}")
    print(f"  {W}Orchestrator — Проверка зависимостей{RST}")
    print(f"{'═' * 60}")

    sp_req  = config.SHORTS_PROJECT_DIR / "requirements.txt"
    orc_req = Path(__file__).parent / "requirements.txt"

    total_fails = 0
    total_fails += check_paths()
    total_fails += check_python_packages("ShortsProject", sp_req)
    total_fails += check_python_packages("Orchestrator",  orc_req)
    total_fails += check_external_tools()
    total_fails += check_ollama()
    total_fails += check_env()
    total_fails += _check_prelend_api()

    print(f"\n{'═' * 60}")
    if total_fails == 0:
        print(f"  {G}✔  Все критические зависимости в порядке. Запуск...{RST}")
    else:
        print(f"  {R}✘  Найдено {total_fails} критических проблем.{RST}")
        print(f"  {Y}Устраните FAIL-ошибки и перезапустите Orchestrator.{RST}")
    print(f"{'═' * 60}\n")

    if total_fails > 0 and abort_on_fail:
        sys.exit(1)

    return total_fails == 0


if __name__ == "__main__":
    run_checks(abort_on_fail=False)
