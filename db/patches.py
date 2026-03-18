"""
db/patches.py — CRUD для таблицы pending_patches.

Жизненный цикл патча:
  pending → approved (оператор: /approve_N) → applied / failed
  pending → rejected (оператор: /reject_N)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from db.connection import get_db

logger = logging.getLogger(__name__)


MAX_PENDING_PATCHES = 20  # Максимум ожидающих патчей — защита от накопления


def save_pending_patch(
    plan_id: int,
    repo: str,
    file_path: str,
    goal: str,
    original_code: str,
    patched_code: str,
    diff_preview: str,
) -> int:
    """Сохраняет патч в статусе 'pending'. Возвращает ID записи."""
    with get_db() as conn:
        # Проверяем лимит ожидающих патчей
        count = conn.execute(
            "SELECT COUNT(*) FROM pending_patches WHERE status IN ('pending', 'approved')"
        ).fetchone()[0]
        if count >= MAX_PENDING_PATCHES:
            logger.warning(
                "[Patches] Лимит ожидающих патчей (%d) достигнут — новый патч отклонён",
                MAX_PENDING_PATCHES,
            )
            return -1

        cur = conn.execute("""
            INSERT INTO pending_patches
                (plan_id, repo, file_path, goal, original_code, patched_code, diff_preview, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
        """, (plan_id, repo, file_path, goal, original_code, patched_code, diff_preview))
        return cur.lastrowid


def get_patch(patch_id: int) -> Optional[Dict]:
    """Возвращает запись патча по ID или None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM pending_patches WHERE id = ?", (patch_id,)
        ).fetchone()
        return dict(row) if row else None


def get_approved_patches() -> List[Dict]:
    """Возвращает все патчи в статусе 'approved' (готовы к применению)."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM pending_patches
            WHERE status = 'approved'
            ORDER BY approved_at ASC
        """).fetchall()
        return [dict(r) for r in rows]


def get_pending_patches() -> List[Dict]:
    """Возвращает все патчи в статусе 'pending' (ожидают решения оператора)."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM pending_patches
            WHERE status = 'pending'
            ORDER BY created_at ASC
        """).fetchall()
        return [dict(r) for r in rows]


def mark_patch_approved(patch_id: int) -> bool:
    """Помечает патч как одобренный. Возвращает False если ID не найден или статус не 'pending'."""
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE pending_patches SET status = 'approved', approved_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (datetime.now(timezone.utc).isoformat(), patch_id),
        )
        return cur.rowcount > 0


def mark_patch_rejected(patch_id: int) -> bool:
    """Помечает патч как отклонённый. Возвращает False если ID не найден или статус не 'pending'."""
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE pending_patches SET status = 'rejected', approved_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (datetime.now(timezone.utc).isoformat(), patch_id),
        )
        return cur.rowcount > 0


def mark_patch_applied(patch_id: int, apply_result: str = "") -> None:
    """Помечает патч как успешно применённый."""
    with get_db() as conn:
        conn.execute(
            "UPDATE pending_patches SET status = 'applied', applied_at = ?, apply_result = ? "
            "WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), apply_result[:2000], patch_id),
        )


def mark_patch_failed(patch_id: int, apply_result: str = "") -> None:
    """Помечает патч как проваленный (тесты не прошли)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE pending_patches SET status = 'failed', applied_at = ?, apply_result = ? "
            "WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), apply_result[:2000], patch_id),
        )
