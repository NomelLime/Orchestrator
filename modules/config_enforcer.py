"""
modules/config_enforcer.py — Безопасное применение изменений конфигов.

Только Zone 1 (scheduling) и Zone 2 (visual) на данном этапе.
Zone 3 (prelend) — TODO.

Каждое изменение:
    1. Читает текущее значение (old_value)
    2. Делает git-бэкап перед изменением
    3. Применяет атомарную запись (write-temp → rename)
    4. Делает git-commit после изменения
    5. Записывает в applied_changes

Экспортирует:
    apply_config_changes(plan, plan_id) → (success_count, fail_count)
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from db.experiences import save_applied_change
from db.zones       import get_all_zones
from modules.zones  import can_apply, record_success, record_failure
from integrations   import shorts_project as sp_integration
from integrations   import prelend as pl_integration
from integrations   import git_tools

logger = logging.getLogger(__name__)


def apply_config_changes(plan: Dict, plan_id: int) -> Tuple[int, int]:
    """
    Применяет все config_changes из плана для обоих репозиториев.

    Returns:
        (success_count, fail_count)
    """
    success = 0
    fail    = 0

    # ── ShortsProject config_changes ─────────────────────────────────────────
    sp_changes = plan.get("targets", {}).get("shorts_project", {}).get("config_changes", [])
    for change in sp_changes:
        zone = change.get("scope", "scheduling")
        if not can_apply(zone):
            logger.info("[ConfigEnforcer] Пропуск SP config (зона '%s' неактивна)", zone)
            continue
        ok = _apply_sp_config_change(change, plan_id, zone)
        if ok:
            success += 1
            record_success(zone, change.get("description", ""))
        else:
            fail += 1
            record_failure(zone, change.get("description", ""))

    # ── PreLend config_changes ────────────────────────────────────────────────
    pl_changes = plan.get("targets", {}).get("prelend", {}).get("config_changes", [])
    for change in pl_changes:
        zone = change.get("scope", "prelend")
        if not can_apply(zone):
            logger.info("[ConfigEnforcer] Пропуск PreLend config (зона '%s' неактивна)", zone)
            continue
        ok = _apply_pl_config_change(change, plan_id, zone)
        if ok:
            success += 1
            record_success(zone, change.get("description", ""))
        else:
            fail += 1
            record_failure(zone, change.get("description", ""))

    return success, fail


# ─────────────────────────────────────────────────────────────────────────────
# ShortsProject
# ─────────────────────────────────────────────────────────────────────────────

def _apply_sp_config_change(change: Dict, plan_id: int, zone: str) -> bool:
    """
    Применяет одно изменение конфига ShortsProject.

    Поддерживаемые scope:
        scheduling → account config.json (upload_schedule)
        visual     → TODO: параметры уникализации

    Returns True при успехе.
    """
    scope       = change.get("scope", "scheduling")
    description = change.get("description", "")

    if scope == "scheduling":
        return _apply_sp_schedule(change, plan_id, zone, description)

    elif scope == "visual":
        # TODO (Zone 2): изменение параметров уникализации
        # Нужно реализовать когда Zone 2 достигнет нужного confidence_score
        logger.info("[ConfigEnforcer] Visual scope — TODO (Zone 2 не реализована)")
        return False

    else:
        logger.warning("[ConfigEnforcer] Неизвестный SP scope: %s", scope)
        return False


def _apply_sp_schedule(
    change: Dict, plan_id: int, zone: str, description: str
) -> bool:
    """
    Обновляет upload_schedule в account config.json аккаунтов ShortsProject.

    Логика аналогична Strategist._apply_schedule_recommendations(),
    но более консервативна: только конкретные аккаунты/платформы из плана.
    """
    platform  = change.get("platform")
    new_times = change.get("new_value") or change.get("new_schedule", [])
    accounts  = change.get("accounts", ["all"])  # ["all"] или конкретные имена

    if not platform or not new_times:
        logger.warning("[ConfigEnforcer] Нет platform или new_value в плане scheduling")
        return False

    try:
        all_accounts = sp_integration.get_all_accounts()
    except Exception as exc:
        logger.error("[ConfigEnforcer] Не удалось загрузить аккаунты SP: %s", exc)
        return False

    updated = 0
    for acc in all_accounts:
        acc_name = acc.get("name", "")

        # Фильтр по аккаунтам
        if accounts != ["all"] and acc_name not in accounts:
            continue

        # Фильтр по платформам
        if platform not in acc.get("platforms", []):
            continue

        cfg_path = Path(acc["dir"]) / "config.json"
        try:
            acc_cfg  = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            acc_cfg  = {}

        old_schedule = acc_cfg.get("upload_schedule", {}).get(platform, [])

        # Нет изменений — пропускаем
        if old_schedule == new_times:
            continue

        # git-бэкап перед изменением
        git_tools.backup_file(cfg_path, repo_dir=config.SHORTS_PROJECT_DIR)

        # Атомарная запись
        acc_cfg.setdefault("upload_schedule", {})[platform] = new_times
        _atomic_write_json(cfg_path, acc_cfg)

        # git-commit после изменения
        git_tools.commit_change(
            repo_dir    = config.SHORTS_PROJECT_DIR,
            file_path   = cfg_path,
            message     = f"[Orchestrator] schedule {acc_name}/{platform}: {new_times}",
        )

        save_applied_change(
            plan_id     = plan_id,
            change_type = "config_change",
            repo        = "ShortsProject",
            zone        = zone,
            description = description or f"schedule {acc_name}/{platform}",
            file_path   = str(cfg_path.relative_to(config.SHORTS_PROJECT_DIR)),
            old_value   = {platform: old_schedule},
            new_value   = {platform: new_times},
            test_status = "skipped",   # config_change не требует pytest
        )

        logger.info("[ConfigEnforcer] SP schedule %s/%s: %s → %s",
                    acc_name, platform, old_schedule, new_times)
        updated += 1

    return updated > 0


# ─────────────────────────────────────────────────────────────────────────────
# PreLend
# ─────────────────────────────────────────────────────────────────────────────

def _apply_pl_config_change(change: Dict, plan_id: int, zone: str) -> bool:
    """
    Применяет изменение конфига PreLend (settings.json или advertisers.json).

    TODO (Zone 3): реализовать конкретные типы изменений.
    Пока только заглушка с логированием.
    """
    scope       = change.get("scope", "")
    description = change.get("description", "")

    # TODO: реализовать изменение порогов alerts в settings.json
    # TODO: реализовать смену CTA/заголовков в шаблонах
    logger.info("[ConfigEnforcer] PreLend config '%s' — TODO (Zone 3): %s", scope, description)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _atomic_write_json(path: Path, data: Dict) -> None:
    """
    Атомарная запись JSON через временный файл (write-temp → rename).
    Та же техника, что используется в AgentMemory обоих проектов.
    Предотвращает повреждение файла при сбое/OOM.
    """
    text = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.write(fd, text.encode("utf-8"))
        os.close(fd)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise
