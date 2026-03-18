"""
modules/config_enforcer.py — Безопасное применение изменений конфигов.

Zone 1 (scheduling) — upload_schedule в account/config.json ShortsProject.
Zone 2 (visual)     — TODO: параметры уникализации видео.
Zone 3 (prelend)    — settings.json (пороги алертов) + advertisers.json (ставки).

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
from modules.agent_healer import snapshot_config
from integrations   import shorts_project as sp_integration
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
    platform   = change.get("platform")
    new_times  = change.get("new_value") or change.get("new_schedule", [])
    accounts   = change.get("accounts", ["all"])  # ["all"] или конкретные имена
    target_geo = change.get("target_geo", "")     # ISO-2 — для конвертации в UTC

    if not platform or not new_times:
        logger.warning("[ConfigEnforcer] Нет platform или new_value в плане scheduling")
        return False

    # Конвертируем prime-time местного ГЕО → UTC
    if target_geo:
        from modules.timezone_mapper import convert_schedule
        utc_times = convert_schedule(new_times, target_geo)
        logger.info(
            "[ConfigEnforcer] Конвертация расписания: %s (local %s) → %s (UTC)",
            new_times, target_geo, utc_times,
        )
        new_times = utc_times

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

        # Снапшот конфига для self-healing
        snapshot_config("SCOUT", str(cfg_path), plan_id)

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
    Применяет изменение конфига PreLend.

    Поддерживаемые scope:
        thresholds      → settings.json → alerts.*  (числовые пороги)
        advertiser_rate → advertisers.json → [id=X].rate
    """
    scope = change.get("scope", "")

    if scope == "thresholds":
        return _apply_pl_thresholds(change, plan_id, zone)
    elif scope == "advertiser_rate":
        return _apply_pl_advertiser_rate(change, plan_id, zone)
    else:
        logger.warning("[ConfigEnforcer] Неизвестный PreLend scope: '%s'", scope)
        return False


# Разрешённые параметры в settings.json → alerts (не даём LLM менять что попало)
_PL_ALLOWED_THRESHOLDS = frozenset({
    "bot_pct_per_hour",
    "offgeo_pct_per_hour",
    "shave_threshold_pct",
    "landing_slow_ms",
    "landing_down_alert_min",
})


def _apply_pl_thresholds(change: Dict, plan_id: int, zone: str) -> bool:
    """
    Обновляет числовой порог в PreLend/config/settings.json → alerts.
    Запись выполняется через Internal API (VPS), не через файловую систему.
    """
    param       = change.get("param", "")
    new_value   = change.get("new_value")
    description = change.get("description", "")

    if param not in _PL_ALLOWED_THRESHOLDS:
        logger.warning("[ConfigEnforcer] PreLend threshold: недопустимый param '%s'", param)
        return False
    if new_value is None:
        logger.warning("[ConfigEnforcer] PreLend threshold: нет new_value для '%s'", param)
        return False

    from integrations.prelend_client import get_client
    client = get_client()

    current = client.get_settings()
    if not current or not isinstance(current, dict):
        logger.error(
            "[ConfigEnforcer] Не удалось прочитать PL settings через API "
            "(пустой или невалидный ответ)"
        )
        return False
    if "alerts" not in current:
        logger.error(
            "[ConfigEnforcer] PL settings не содержит ключ 'alerts' — "
            "возможно ошибка API или пустой файл"
        )
        return False

    old_value = current.get("alerts", {}).get(param)
    if old_value == new_value:
        logger.debug("[ConfigEnforcer] PL threshold %s не изменился (%s)", param, new_value)
        return True

    current.setdefault("alerts", {})[param] = new_value

    ok = client.write_settings(
        current,
        source=f"orchestrator/plan_{plan_id}",
    )
    if not ok:
        logger.error("[ConfigEnforcer] Не удалось записать PL settings через API")
        return False

    # git commit выполняется на стороне VPS (внутри Internal API)
    save_applied_change(
        plan_id     = plan_id,
        change_type = "config_change",
        repo        = "PreLend",
        zone        = zone,
        description = description or f"threshold {param}",
        file_path   = "config/settings.json",
        old_value   = {"alerts": {param: old_value}},
        new_value   = {"alerts": {param: new_value}},
        test_status = "skipped",
    )
    logger.info("[ConfigEnforcer] PL threshold %s: %s → %s (via API)", param, old_value, new_value)
    return True


def _apply_pl_advertiser_rate(change: Dict, plan_id: int, zone: str) -> bool:
    """
    Изменяет поле rate рекламодателя в PreLend/config/advertisers.json.
    Запись выполняется через Internal API (VPS).
    """
    advertiser_id = change.get("advertiser_id") or change.get("param", "")
    new_rate      = change.get("new_value")
    description   = change.get("description", "")

    if not advertiser_id or new_rate is None:
        logger.warning("[ConfigEnforcer] PreLend advertiser_rate: нужны advertiser_id и new_value")
        return False

    from integrations.prelend_client import get_client
    client = get_client()

    advertisers = client.get_advertisers()
    if not isinstance(advertisers, list) or len(advertisers) == 0:
        logger.error(
            "[ConfigEnforcer] Не удалось прочитать PL advertisers через API "
            "(пустой или невалидный ответ) — запись отменена"
        )
        return False

    target = next((a for a in advertisers if a.get("id") == advertiser_id), None)
    if target is None:
        logger.warning("[ConfigEnforcer] PreLend: рекламодатель '%s' не найден", advertiser_id)
        return False

    old_rate = target.get("rate")
    if old_rate == new_rate:
        logger.debug("[ConfigEnforcer] PL rate %s не изменился (%s)", advertiser_id, new_rate)
        return True

    target["rate"] = new_rate
    ok = client.write_advertisers(
        advertisers,
        source=f"orchestrator/plan_{plan_id}",
    )
    if not ok:
        logger.error("[ConfigEnforcer] Не удалось записать PL advertisers через API")
        return False

    save_applied_change(
        plan_id     = plan_id,
        change_type = "config_change",
        repo        = "PreLend",
        zone        = zone,
        description = description or f"rate {advertiser_id}",
        file_path   = "config/advertisers.json",
        old_value   = {"id": advertiser_id, "rate": old_rate},
        new_value   = {"id": advertiser_id, "rate": new_rate},
        test_status = "skipped",
    )
    logger.info("[ConfigEnforcer] PL rate %s: %s → %s (via API)", advertiser_id, old_rate, new_rate)
    return True


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
