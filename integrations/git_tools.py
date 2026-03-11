"""
integrations/git_tools.py — Безопасная работа с git.

Все операции git выполняются как subprocess — не требует gitpython.
При ошибке git логируем предупреждение, но не бросаем исключение:
Orchestrator должен продолжать работу даже если git недоступен.

Экспортирует:
    backup_file(file_path, repo_dir)       → создаёт git stash / commit backup
    commit_change(repo_dir, file_path, message) → git add + commit
    get_last_commit_hash(repo_dir)         → str хэш или ""
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)


def _run_git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    """Выполняет git команду. Логирует ошибки, не бросает исключений."""
    try:
        return subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        logger.warning("[Git] Ошибка выполнения git %s: %s", " ".join(args), exc)
        # Возвращаем фейковый результат с ошибкой
        result = subprocess.CompletedProcess(args, returncode=1)
        result.stdout = ""
        result.stderr = str(exc)
        return result


def backup_file(file_path: Path, repo_dir: Path) -> bool:
    """
    Создаёт git-бэкап перед изменением файла.
    Стратегия: убеждаемся что текущее состояние файла закоммичено.
    Если файл не изменён — бэкап не нужен.
    Если изменён — делаем backup-commit.
    """
    if not config.GIT_AUTOCOMMIT:
        return True

    # Проверяем, изменён ли файл
    rel_path = file_path.relative_to(repo_dir)
    result = _run_git(["diff", "--name-only", str(rel_path)], cwd=repo_dir)
    if result.returncode != 0:
        return False

    if not result.stdout.strip():
        # Файл не изменён — бэкап не нужен
        return True

    # Файл изменён — сохраняем как backup-commit
    msg = f"[Orchestrator/backup] pre-change backup: {rel_path}"
    return _do_commit(repo_dir, [str(rel_path)], msg)


def commit_change(repo_dir: Path, file_path: Path, message: str) -> bool:
    """
    Делает git add + commit для указанного файла.
    Добавляет подпись Orchestrator в commit message.
    """
    if not config.GIT_AUTOCOMMIT:
        return True

    try:
        rel_path = str(file_path.relative_to(repo_dir))
    except ValueError:
        rel_path = str(file_path)

    full_message = f"{message}\n\nAuthor: {config.GIT_AUTHOR}"
    return _do_commit(repo_dir, [rel_path], full_message)


def _do_commit(repo_dir: Path, files: list, message: str) -> bool:
    """Внутренний helper: git add files + git commit."""
    # git add
    add_result = _run_git(["add"] + files, cwd=repo_dir)
    if add_result.returncode != 0:
        logger.warning("[Git] git add failed: %s", add_result.stderr[:200])
        return False

    # git commit
    commit_result = _run_git(["commit", "-m", message], cwd=repo_dir)
    if commit_result.returncode != 0:
        # "nothing to commit" — не ошибка
        if "nothing to commit" in commit_result.stdout + commit_result.stderr:
            return True
        logger.warning("[Git] git commit failed: %s", commit_result.stderr[:200])
        return False

    logger.info("[Git] Commit: %s", message[:60])
    return True


def get_last_commit_hash(repo_dir: Path) -> str:
    """Возвращает хэш последнего коммита или пустую строку."""
    result = _run_git(["rev-parse", "--short", "HEAD"], cwd=repo_dir)
    return result.stdout.strip() if result.returncode == 0 else ""


def find_last_orc_commit(repo_dir: Path) -> str:
    """
    Ищет хэш последнего коммита с меткой [Orchestrator...] в SP-репозитории.
    Возвращает short hash или пустую строку.
    """
    result = _run_git(
        ["log", "--oneline", "--grep", "[Orchestrator", "-1"],
        cwd=repo_dir,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    # Формат: "abc1234 [Orchestrator/CodeEvolver] описание"
    parts = result.stdout.strip().split(" ", 1)
    return parts[0] if parts else ""


def revert_commit(repo_dir: Path, commit_hash: str) -> bool:
    """
    Откатывает коммит через git revert --no-edit (создаёт revert-коммит).
    Возвращает True при успехе.
    """
    if not commit_hash:
        return False
    result = _run_git(
        ["revert", commit_hash, "--no-edit"],
        cwd=repo_dir,
    )
    if result.returncode != 0:
        logger.warning("[Git] revert %s не удался: %s", commit_hash, result.stderr[:200])
        return False
    logger.info("[Git] Откат коммита %s выполнен", commit_hash)
    return True
