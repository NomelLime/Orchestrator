"""
modules/code_evolver.py — Zone 4: автоматические патчи кода.

ОГРАНИЧЕНИЯ (намеренные, см. config.py):
    - Только .py файлы
    - Только ShortsProject репозиторий
    - Максимум CODE_EVOLVER_MAX_PATCH_LINES строк на патч
    - Требует прохождения pytest перед применением
    - При падении тестов — обязательный rollback

Пайплайн для каждого патча:
    1. Прочитать исходный файл
    2. Сгенерировать патч через Qwen-coder (Ollama)
    3. Применить патч к копии файла
    4. Запустить pytest
    5. Если тесты прошли → заменить оригинал + git commit
    6. Если тесты упали → rollback + запись отрицательного опыта

Экспортирует:
    apply_code_patches(plan, plan_id) → (success_count, fail_count)
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import config
from db.experiences  import save_applied_change
from modules.zones   import can_apply, record_success, record_failure
from integrations    import git_tools
from integrations.ollama_client import call_llm

logger = logging.getLogger(__name__)


def apply_code_patches(plan: Dict, plan_id: int) -> Tuple[int, int]:
    """
    Применяет все code_patches из плана.
    Поддерживает только ShortsProject Python-файлы (Zone 4).

    Returns:
        (success_count, fail_count)
    """
    success = 0
    fail    = 0

    if not can_apply("code"):
        logger.info("[CodeEvolver] Zone 'code' неактивна — патчи пропущены")
        return 0, 0

    # ShortsProject patches
    sp_patches = plan.get("targets", {}).get("shorts_project", {}).get("code_patches", [])
    for patch_spec in sp_patches:
        ok = _apply_single_patch(patch_spec, plan_id, repo="ShortsProject")
        if ok:
            success += 1
        else:
            fail += 1

    # PreLend patches — ЗАБЛОКИРОВАНЫ
    pl_patches = plan.get("targets", {}).get("prelend", {}).get("code_patches", [])
    if pl_patches:
        logger.warning(
            "[CodeEvolver] PreLend code patches заблокированы (PHP + слабые тесты). "
            "Пропущено %d патчей.", len(pl_patches)
        )

    return success, fail


def _apply_single_patch(patch_spec: Dict, plan_id: int, repo: str) -> bool:
    """
    Полный пайплайн применения одного патча.

    patch_spec структура:
        file: "pipeline/agents/editor.py"
        goal: "Снизить агрессивность шума"
        patch_format: "unified_diff" | "full_file"

    Returns True если патч применён успешно.
    """
    file_rel  = patch_spec.get("file", "")
    goal      = patch_spec.get("goal", "")

    # ── Валидация ─────────────────────────────────────────────────────────────
    if not file_rel or not goal:
        logger.warning("[CodeEvolver] Пустой file или goal в патче")
        return False

    file_path = config.SHORTS_PROJECT_DIR / file_rel
    if not file_path.exists():
        logger.warning("[CodeEvolver] Файл не найден: %s", file_path)
        return False

    if file_path.suffix not in config.CODE_EVOLVER_ALLOWED_EXTENSIONS:
        logger.warning("[CodeEvolver] Расширение %s не разрешено", file_path.suffix)
        return False

    # ── Читаем оригинальный файл ──────────────────────────────────────────────
    try:
        original_code = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.error("[CodeEvolver] Не удалось прочитать %s: %s", file_path, exc)
        return False

    # ── Генерируем патч через Qwen-coder ──────────────────────────────────────
    logger.info("[CodeEvolver] Генерация патча для %s: %s", file_rel, goal)
    patched_code = _generate_patched_code(original_code, goal, file_rel)
    if not patched_code:
        logger.warning("[CodeEvolver] LLM не вернула патч для %s", file_rel)
        return False

    # Проверка размера патча
    original_lines = original_code.count("\n")
    patched_lines  = patched_code.count("\n")
    delta_lines    = abs(patched_lines - original_lines)
    if delta_lines > config.CODE_EVOLVER_MAX_PATCH_LINES:
        logger.warning(
            "[CodeEvolver] Патч слишком большой (%d строк изменений > %d лимит) — отклонён",
            delta_lines, config.CODE_EVOLVER_MAX_PATCH_LINES,
        )
        return False

    # ── Создаём временную копию и применяем патч ──────────────────────────────
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", encoding="utf-8", delete=False
    ) as tmp_f:
        tmp_path = Path(tmp_f.name)
        tmp_f.write(patched_code)

    try:
        # ── Запускаем тесты на временном файле ───────────────────────────────
        # Подменяем оригинальный файл временным, гоняем pytest, откатываем
        backup_path = file_path.with_suffix(".py.bak")
        shutil.copy2(file_path, backup_path)

        try:
            shutil.copy2(tmp_path, file_path)
            test_ok, test_output = _run_tests()
        except Exception as exc:
            test_ok     = False
            test_output = str(exc)
        finally:
            # Откатываем в любом случае, потом решаем: оставить или нет
            if not test_ok:
                shutil.copy2(backup_path, file_path)

        # ── Решение по результатам тестов ─────────────────────────────────────
        if test_ok:
            # Заменяем оригинал патченым файлом (уже сделано выше через copy2)
            shutil.copy2(tmp_path, file_path)

            git_tools.commit_change(
                repo_dir  = config.SHORTS_PROJECT_DIR,
                file_path = file_path,
                message   = f"[Orchestrator/CodeEvolver] {goal[:72]}",
            )

            save_applied_change(
                plan_id     = plan_id,
                change_type = "code_patch",
                repo        = repo,
                zone        = "code",
                description = goal,
                file_path   = file_rel,
                test_status = "passed",
                test_output = test_output[:2000],
                rolled_back = False,
            )

            record_success("code", goal)
            logger.info("[CodeEvolver] ✅ Патч применён: %s — %s", file_rel, goal)
            backup_path.unlink(missing_ok=True)
            return True

        else:
            # Rollback уже сделан в finally выше
            save_applied_change(
                plan_id        = plan_id,
                change_type    = "code_patch",
                repo           = repo,
                zone           = "code",
                description    = goal,
                file_path      = file_rel,
                test_status    = "failed",
                test_output    = test_output[:2000],
                rolled_back    = True,
                rollback_reason= "pytest провалил тесты",
            )

            record_failure("code", f"тесты упали: {goal}")
            logger.warning("[CodeEvolver] ❌ Патч отклонён (тесты): %s — %s", file_rel, goal)
            backup_path.unlink(missing_ok=True)
            return False

    finally:
        tmp_path.unlink(missing_ok=True)


def _generate_patched_code(original_code: str, goal: str, file_name: str) -> Optional[str]:
    """
    Запрашивает у Qwen-coder изменённую версию файла.
    Просит вернуть ТОЛЬКО полный код файла без пояснений.
    """
    prompt = (
        f"Ты — Python-разработчик. Тебе нужно изменить файл {file_name}.\n"
        f"Цель изменения: {goal}\n\n"
        f"Правила:\n"
        f"1. Верни ТОЛЬКО полный исходный код файла после изменений\n"
        f"2. Никаких пояснений, markdown-блоков, комментариев про изменения\n"
        f"3. Не добавляй функциональность сверх поставленной цели\n"
        f"4. Сохрани все импорты и существующий API\n\n"
        f"Текущий код файла:\n"
        f"```python\n{original_code}\n```\n\n"
        f"Верни изменённый код:"
    )

    raw = call_llm(model=config.OLLAMA_CODE_MODEL, prompt=prompt)
    if not raw:
        return None

    # Убираем markdown-обёртки если LLM добавила
    import re
    clean = re.sub(r"^```(?:python)?\s*", "", raw.strip())
    clean = re.sub(r"\s*```$", "", clean)
    return clean.strip()


def _run_tests() -> Tuple[bool, str]:
    """
    Запускает pytest для ShortsProject.
    Returns (passed: bool, output: str).
    """
    try:
        result = subprocess.run(
            config.SP_PYTEST_CMD,
            capture_output = True,
            text           = True,
            timeout        = 120,
            cwd            = str(config.SHORTS_PROJECT_DIR),
        )
        output  = result.stdout + result.stderr
        passed  = result.returncode == 0
        return passed, output
    except subprocess.TimeoutExpired:
        return False, "pytest timeout (120s)"
    except Exception as exc:
        return False, f"pytest error: {exc}"
