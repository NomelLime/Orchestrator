"""
modules/code_evolver.py — Zone 4: патчи кода через Telegram-апрув.

ОГРАНИЧЕНИЯ (намеренные, см. config.py):
    - Только .py файлы
    - Только ShortsProject репозиторий
    - Максимум CODE_EVOLVER_MAX_PATCH_LINES строк на патч
    - Требует прохождения pytest перед применением
    - При падении тестов — обязательный rollback

Пайплайн (двухшаговый, без авто-применения):

  queue_code_patches(plan, plan_id)    → (queued_count)
      1. Читает исходный файл
      2. Генерирует патч через Qwen-coder (Ollama)
      3. Сохраняет в pending_patches со статусом 'pending'
      4. Отправляет diff в Telegram — оператор отвечает /approve_N или /reject_N

  apply_approved_patches()             → (success_count, fail_count)
      1. Берёт все патчи со статусом 'approved' из pending_patches
      2. Применяет: tmpfile → pytest → git commit или rollback
      3. Помечает как 'applied' или 'failed'

Экспортирует:
    queue_code_patches(plan, plan_id)   → int
    apply_approved_patches()            → (int, int)
    check_and_revert_on_crash()         → bool (True если откат выполнен)
"""

from __future__ import annotations

import difflib
import logging
import shutil
import subprocess
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import config
from db.experiences  import save_applied_change
from db.connection   import get_db
from db.patches      import (
    save_pending_patch, get_approved_patches,
    mark_patch_applied, mark_patch_failed,
)
from modules.zones   import can_apply, record_success, record_failure
from integrations    import git_tools
from integrations.shorts_project import get_crash_loop_agents
from integrations.ollama_client import call_llm

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 1 цикла: генерация патчей и отправка на одобрение
# ─────────────────────────────────────────────────────────────────────────────

def queue_code_patches(plan: Dict, plan_id: int) -> int:
    """
    Генерирует патчи кода из плана, сохраняет в БД и отправляет diff в Telegram.
    Патчи НЕ применяются автоматически — ждут /approve_N от оператора.

    Returns:
        Количество поставленных в очередь патчей.
    """
    if not can_apply("code"):
        logger.info("[CodeEvolver] Zone 'code' неактивна — патчи пропущены")
        return 0

    # ShortsProject patches
    sp_patches = plan.get("targets", {}).get("shorts_project", {}).get("code_patches", [])
    queued = 0
    for patch_spec in sp_patches:
        ok = _queue_single_patch(patch_spec, plan_id, repo="ShortsProject")
        if ok:
            queued += 1

    # PreLend patches — ЗАБЛОКИРОВАНЫ
    pl_patches = plan.get("targets", {}).get("prelend", {}).get("code_patches", [])
    if pl_patches:
        logger.warning(
            "[CodeEvolver] PreLend code patches заблокированы (PHP + слабые тесты). "
            "Пропущено %d патчей.", len(pl_patches)
        )

    if queued:
        logger.info("[CodeEvolver] Поставлено в очередь: %d патч(ей) — ожидают /approve_N", queued)

    return queued


def _queue_single_patch(patch_spec: Dict, plan_id: int, repo: str) -> bool:
    """
    Генерирует патч для одного файла, сохраняет в pending_patches,
    отправляет diff в Telegram.

    Returns True если патч успешно поставлен в очередь.
    """
    file_rel = patch_spec.get("file", "")
    goal     = patch_spec.get("goal", "")

    # ── Валидация ─────────────────────────────────────────────────────────────
    if not file_rel or not goal:
        logger.warning("[CodeEvolver] Пустой file или goal в патче")
        return False

    # Санитизация перед вставкой в промпт — защита от prompt-injection
    goal     = _sanitize_for_prompt(goal,     max_len=300)
    file_rel = _sanitize_for_prompt(file_rel, max_len=150)

    # Защита от path traversal — LLM не должна выходить за пределы SP
    file_path = (config.SHORTS_PROJECT_DIR / file_rel).resolve()
    try:
        file_path.relative_to(config.SHORTS_PROJECT_DIR.resolve())
    except ValueError:
        logger.error(
            "[CodeEvolver] Path traversal отклонён: %s выходит за пределы ShortsProject", file_rel
        )
        return False

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

    # Проверка размера патча — считаем реально изменённые строки через unified_diff
    diff_lines = list(difflib.unified_diff(
        original_code.splitlines(keepends=True),
        patched_code.splitlines(keepends=True),
        n=0,
    ))
    changed_lines = sum(
        1 for ln in diff_lines
        if (ln.startswith("+") or ln.startswith("-")) and not ln.startswith(("+++", "---"))
    )
    if changed_lines > config.CODE_EVOLVER_MAX_PATCH_LINES:
        logger.warning(
            "[CodeEvolver] Патч слишком большой (%d изменённых строк > лимит %d) — отклонён",
            changed_lines, config.CODE_EVOLVER_MAX_PATCH_LINES,
        )
        return False

    # ── Строим unified diff для Telegram ──────────────────────────────────────
    diff_preview = _build_diff_preview(original_code, patched_code, file_rel)

    # ── Сохраняем в БД ────────────────────────────────────────────────────────
    patch_id = save_pending_patch(
        plan_id       = plan_id,
        repo          = repo,
        file_path     = file_rel,
        goal          = goal,
        original_code = original_code,
        patched_code  = patched_code,
        diff_preview  = diff_preview,
    )
    if patch_id < 0:
        logger.warning("[CodeEvolver] Лимит ожидающих патчей достигнут — %s пропущен", file_rel)
        return False

    # ── Отправляем в Telegram ─────────────────────────────────────────────────
    _notify_patch_pending(patch_id, plan_id, file_rel, goal, diff_preview)
    logger.info("[CodeEvolver] 📬 Патч #%d поставлен в очередь: %s — %s", patch_id, file_rel, goal)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 2 цикла: применение одобренных патчей
# ─────────────────────────────────────────────────────────────────────────────

def apply_approved_patches() -> Tuple[int, int]:
    """
    Применяет все патчи со статусом 'approved'.
    Требует прохождения pytest; при провале — rollback.

    Returns:
        (success_count, fail_count)
    """
    approved = get_approved_patches()
    if not approved:
        return 0, 0

    logger.info("[CodeEvolver] Одобренных патчей к применению: %d", len(approved))
    success = 0
    fail    = 0

    for patch in approved:
        ok = _apply_approved_patch(patch)
        if ok:
            success += 1
        else:
            fail += 1

    return success, fail


def _apply_approved_patch(patch: Dict) -> bool:
    """
    Применяет одобренный патч: tmpfile → pytest → git commit или rollback.
    Обновляет статус в pending_patches и записывает в applied_changes.

    Returns True если патч применён успешно.
    """
    patch_id  = patch["id"]
    file_rel  = patch["file_path"]
    goal      = patch["goal"]
    plan_id   = patch["plan_id"]
    repo      = patch.get("repo", "ShortsProject")

    # Защита от path traversal при применении
    file_path = (config.SHORTS_PROJECT_DIR / file_rel).resolve()
    try:
        file_path.relative_to(config.SHORTS_PROJECT_DIR.resolve())
    except ValueError:
        logger.error("[CodeEvolver] Path traversal при применении: %s", file_rel)
        mark_patch_failed(patch_id, "path traversal отклонён")
        return False

    if not file_path.exists():
        logger.warning("[CodeEvolver] Файл не найден при применении: %s", file_path)
        mark_patch_failed(patch_id, "файл не найден")
        return False

    patched_code = patch["patched_code"]

    # ── Создаём временную копию ───────────────────────────────────────────────
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", encoding="utf-8", delete=False
    ) as tmp_f:
        tmp_path = Path(tmp_f.name)
        tmp_f.write(patched_code)

    try:
        backup_path = file_path.with_suffix(".py.bak")
        shutil.copy2(file_path, backup_path)

        # Тестируем на КОПИИ: подменяем файл → pytest → откат если провал
        try:
            shutil.copy2(tmp_path, file_path)
            test_ok, test_output = _run_tests()
        except Exception as exc:
            test_ok     = False
            test_output = str(exc)
        finally:
            # Всегда восстанавливаем оригинал после тестов
            shutil.copy2(backup_path, file_path)

        # ── Решение по результатам тестов ─────────────────────────────────────
        if test_ok:
            # Записываем патч только ПОСЛЕ успешного прохождения тестов
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

            mark_patch_applied(patch_id, test_output[:500])
            record_success("code", goal)

            logger.info("[CodeEvolver] ✅ Патч #%d применён: %s — %s", patch_id, file_rel, goal)
            _notify_patch_applied(patch_id, file_rel, goal)
            backup_path.unlink(missing_ok=True)
            return True

        else:
            save_applied_change(
                plan_id         = plan_id,
                change_type     = "code_patch",
                repo            = repo,
                zone            = "code",
                description     = goal,
                file_path       = file_rel,
                test_status     = "failed",
                test_output     = test_output[:2000],
                rolled_back     = True,
                rollback_reason = "pytest провалил тесты",
            )

            mark_patch_failed(patch_id, test_output[:500])
            record_failure("code", f"тесты упали: {goal}")

            logger.warning("[CodeEvolver] ❌ Патч #%d провалил тесты: %s — %s", patch_id, file_rel, goal)
            _notify_patch_failed(patch_id, file_rel, goal, test_output)
            backup_path.unlink(missing_ok=True)
            return False

    finally:
        tmp_path.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Краш-луп детектор (без изменений)
# ─────────────────────────────────────────────────────────────────────────────

def check_and_revert_on_crash() -> bool:
    """
    Проверяет краш-луп агентов ShortsProject.
    Если краш-луп обнаружен — откатывает последний Orchestrator-коммит в SP через git revert.

    Логика:
      1. Читает agent_memory.json через get_crash_loop_agents()
      2. Если есть агент с 3+ restart_requested за последний час —
         ищет последний коммит [Orchestrator/...] в SP
      3. Если коммит найден — git revert (создаёт revert-коммит)
      4. Помечает изменение в БД как rolled_back, пишет в notifier

    Возвращает True если откат был выполнен.
    """
    crash_agents = get_crash_loop_agents(
        window_minutes     = config.CRASH_LOOP_WINDOW_MIN,
        min_restart_requests = config.CRASH_LOOP_MIN_RESTARTS,
    )
    if not crash_agents:
        return False

    agents_str = ", ".join(crash_agents)
    logger.warning("[CodeEvolver] Краш-луп обнаружен: %s — ищу последний патч для отката", agents_str)

    commit_hash = git_tools.find_last_orc_commit(config.SHORTS_PROJECT_DIR)
    if not commit_hash:
        logger.warning("[CodeEvolver] Краш-луп есть, но Orchestrator-коммитов не найдено — откат невозможен")
        _notify_crash_no_commit(agents_str)
        return False

    # Проверяем что коммит сделан в пределах окна краш-лупа —
    # откатывать старый коммит (до начала лупа) нет смысла
    commit_ts  = git_tools.get_commit_timestamp(config.SHORTS_PROJECT_DIR, commit_hash)
    window_start = time.time() - config.CRASH_LOOP_WINDOW_MIN * 60
    if commit_ts and commit_ts < window_start:
        logger.warning(
            "[CodeEvolver] Коммит %s слишком старый (ts=%d, window_start=%d) — "
            "откат не нужен, краш-луп не связан с последним патчем",
            commit_hash, commit_ts, int(window_start),
        )
        return False
    logger.info(
        "[CodeEvolver] Коммит %s попадает в окно краш-лупа (ts=%d >= window_start=%d) — откат обоснован",
        commit_hash, commit_ts, int(window_start),
    )

    reverted = git_tools.revert_commit(config.SHORTS_PROJECT_DIR, commit_hash)
    if not reverted:
        logger.error("[CodeEvolver] git revert %s не удался", commit_hash)
        _notify_crash_revert_failed(agents_str, commit_hash)
        return False

    _mark_last_patch_reverted(crash_agents, commit_hash)
    record_failure("code", f"краш-луп агентов: {agents_str} → откат {commit_hash}")
    _notify_crash_reverted(agents_str, commit_hash)
    logger.info("[CodeEvolver] ✅ Откат %s выполнен (краш-луп: %s)", commit_hash, agents_str)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные
# ─────────────────────────────────────────────────────────────────────────────

def sanitize_for_prompt(value: str, max_len: int = 500) -> str:
    """
    Очищает строку перед вставкой в LLM-промпт:
      - удаляет ASCII и Unicode control/format символы (категории Cc, Cf)
        включая нулевые байты, управляющие escape-последовательности,
        Unicode direction overrides (U+202E) и прочие невидимые инжекции
      - обрезает до max_len символов
    Предотвращает prompt-injection через поля goal, file_name, agent_memory.
    """
    cleaned = ''.join(c for c in value if unicodedata.category(c) not in ('Cc', 'Cf'))
    return cleaned[:max_len]


# Алиас для обратной совместимости с внутренними вызовами
_sanitize_for_prompt = sanitize_for_prompt


def _build_diff_preview(original: str, patched: str, filename: str, max_chars: int = 2500) -> str:
    """Строит unified diff для отображения в Telegram."""
    diff_lines = list(difflib.unified_diff(
        original.splitlines(keepends=True),
        patched.splitlines(keepends=True),
        fromfile=filename,
        tofile=f"{filename} (patched)",
        n=3,
    ))
    diff_str = "".join(diff_lines)
    if len(diff_str) > max_chars:
        diff_str = diff_str[:max_chars] + "\n...(diff обрезан)"
    return diff_str


def _generate_patched_code(original_code: str, goal: str, file_name: str) -> Optional[str]:
    """Запрашивает у Qwen-coder изменённую версию файла."""
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

    import re
    clean = re.sub(r"^```(?:python)?\s*", "", raw.strip())
    clean = re.sub(r"\s*```$", "", clean)
    return clean.strip()


def _run_tests() -> Tuple[bool, str]:
    """Запускает pytest для ShortsProject. Returns (passed: bool, output: str)."""
    try:
        result = subprocess.run(
            config.SP_PYTEST_CMD,
            capture_output = True,
            text           = True,
            timeout        = 120,
            cwd            = str(config.SHORTS_PROJECT_DIR),
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "pytest timeout (120s)"
    except Exception as exc:
        return False, f"pytest error: {exc}"


def _mark_last_patch_reverted(crash_agents: list, commit_hash: str = "") -> None:
    """
    Помечает применённое изменение как rolled_back в applied_changes.
    При наличии commit_hash — ищет по нему (точнее, чем по change_type).
    Fallback: последний code_patch без rolled_back.
    """
    reason = f"краш-луп: {', '.join(crash_agents)}"
    try:
        with get_db() as conn:
            row = None
            if commit_hash:
                # Ищем по коротким 8 символам хэша в description
                row = conn.execute("""
                    SELECT id FROM applied_changes
                    WHERE rolled_back = 0 AND description LIKE ?
                    ORDER BY applied_at DESC LIMIT 1
                """, (f"%{commit_hash[:8]}%",)).fetchone()

            if not row:
                # Fallback: последний code_patch
                row = conn.execute("""
                    SELECT id FROM applied_changes
                    WHERE change_type = 'code_patch' AND rolled_back = 0
                    ORDER BY applied_at DESC LIMIT 1
                """).fetchone()

            if row:
                conn.execute("""
                    UPDATE applied_changes
                    SET rolled_back = 1, rollback_reason = ?
                    WHERE id = ?
                """, (reason, row["id"]))
                logger.info("[CodeEvolver] applied_changes #%d помечен как откатанный", row["id"])
            else:
                logger.warning("[CodeEvolver] Нет записей applied_changes для пометки отката")
    except Exception as exc:
        logger.warning("[CodeEvolver] Не удалось пометить откат в БД: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Telegram-уведомления
# ─────────────────────────────────────────────────────────────────────────────

def _notify_patch_pending(
    patch_id: int, plan_id: int, file_rel: str, goal: str, diff_preview: str
) -> None:
    from commander import notifier
    # Экранируем diff для HTML — показываем в <pre> только первые 2000 символов
    safe_diff = diff_preview[:2000].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    notifier.send_message(
        f"📝 <b>Code Patch #{patch_id} — план #{plan_id}</b>\n"
        f"📄 Файл: <code>{file_rel}</code>\n"
        f"🎯 Цель: {goal}\n\n"
        f"<pre>{safe_diff}</pre>\n\n"
        f"✅ <b>/approve_{patch_id}</b> — применить\n"
        f"❌ <b>/reject_{patch_id}</b> — отклонить"
    )
    notifier.log_notification(
        f"Патч #{patch_id} ожидает одобрения: {file_rel} — {goal}",
        level="info", category="patch",
    )


def _notify_patch_applied(patch_id: int, file_rel: str, goal: str) -> None:
    from commander import notifier
    notifier.send_message(
        f"✅ <b>Патч #{patch_id} применён</b>\n"
        f"📄 {file_rel}\n"
        f"🎯 {goal}"
    )
    notifier.log_notification(
        f"Патч #{patch_id} применён: {file_rel}",
        level="info", category="patch",
    )


def _notify_patch_failed(patch_id: int, file_rel: str, goal: str, test_output: str) -> None:
    from commander import notifier
    short_out = test_output[-500:] if len(test_output) > 500 else test_output
    notifier.send_message(
        f"❌ <b>Патч #{patch_id} провалил тесты — откат</b>\n"
        f"📄 {file_rel}\n"
        f"🎯 {goal}\n\n"
        f"<code>{short_out[:400]}</code>"
    )
    notifier.log_notification(
        f"Патч #{patch_id} откатан (тесты): {file_rel}",
        level="warning", category="patch",
    )


def _notify_crash_reverted(agents_str: str, commit_hash: str) -> None:
    from commander import notifier
    notifier.send_message(
        f"🔄 <b>Orchestrator: автооткат патча</b>\n"
        f"Краш-луп агентов: <b>{agents_str}</b>\n"
        f"Откатан коммит: <code>{commit_hash}</code>\n"
        f"Зона 'code' получила штраф."
    )
    notifier.log_notification(
        f"Автооткат {commit_hash}: краш-луп {agents_str}",
        level="warning", category="patch",
    )


def _notify_crash_no_commit(agents_str: str) -> None:
    from commander import notifier
    notifier.send_message(
        f"⚠️ <b>Orchestrator: краш-луп без патча</b>\n"
        f"Агенты в краш-лупе: <b>{agents_str}</b>\n"
        f"Последних Orchestrator-коммитов не найдено."
    )


def _notify_crash_revert_failed(agents_str: str, commit_hash: str) -> None:
    from commander import notifier
    notifier.send_message(
        f"🔴 <b>Orchestrator: откат не удался</b>\n"
        f"Краш-луп: <b>{agents_str}</b>\n"
        f"git revert <code>{commit_hash}</code> вернул ошибку — требуется ручное вмешательство."
    )
