"""
modules/supply_tracker.py — Мониторинг прокси и управление закупками.

Анализирует состояние прокси ShortsProject (через mobileproxy.space API)
и запрашивает подтверждение оператора в Telegram при необходимости.

Флоу подтверждения:
  1. check_supply() обнаруживает проблему → сохраняет proxy_events с
     status='awaiting_confirmation' → отправляет Telegram-сообщение с ID события
  2. Оператор отвечает: "да <event_id>" или "нет <event_id>"
  3. telegram_bot.py вызывает confirm_purchase(id) или reject_purchase(id)
  4. confirm_purchase() выполняет покупку/продление через proxy_manager

Экспортирует:
    check_supply(sp_metrics) → int (кол-во отправленных запросов)
    get_pending_purchase()   → dict или None
    confirm_purchase(id)     → bool
    reject_purchase(id)      → None
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

import config
from db.connection  import get_db
from commander      import notifier
from integrations   import proxy_manager

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Основная проверка (вызывается из main_orchestrator.py раз в N циклов)
# ─────────────────────────────────────────────────────────────────────────────

def check_supply(sp_metrics: Dict) -> int:
    """
    Проверяет состояние прокси и отправляет запросы оператору при необходимости.
    Возвращает количество отправленных запросов.
    Не отправляет новый запрос, если предыдущий ещё не обработан.
    """
    if not config.MOBILEPROXY_API_KEY:
        logger.debug("[SupplyTracker] ORC_MOBILEPROXY_API_KEY не задан — пропуск")
        return 0

    # Не спамить — ждём пока оператор ответит на текущий запрос
    if get_pending_purchase():
        logger.debug("[SupplyTracker] Ожидаем ответа оператора на предыдущий запрос")
        return 0

    sent = 0

    # ── 1. Проверка баланса ───────────────────────────────────────────────────
    balance = proxy_manager.get_balance()
    if balance is not None and balance < config.PROXY_MIN_BALANCE_RUB:
        notifier.send_message(
            f"⚠️ <b>Mobileproxy: низкий баланс</b>\n"
            f"Остаток: <b>{balance:.0f} руб.</b> (минимум {config.PROXY_MIN_BALANCE_RUB:.0f})\n"
            f"Пополни на mobileproxy.space"
        )
        notifier.log_notification(
            f"Баланс прокси {balance:.0f} руб. < {config.PROXY_MIN_BALANCE_RUB:.0f} руб.",
            level="warning", category="metric",
        )

    # ── 2. Истекающие прокси → запрос на продление ───────────────────────────
    expiring = proxy_manager.get_expiring_proxies(within_days=config.PROXY_EXPIRY_WARN_DAYS)
    for proxy in expiring:
        pid    = proxy.get("proxy_id") or proxy.get("id", "?")
        geo    = proxy.get("proxy_geo", "?")
        exp    = (proxy.get("proxy_exp") or "?")[:10]   # только дата
        geoid  = proxy.get("geoid")

        cost     = proxy_manager.estimate_purchase(geo_id=geoid, num=1, period=30) if geoid else None
        cost_str = f"{cost:.0f} руб." if cost else "неизвестно"

        event_id = _save_event(
            event_type="renewal_request",
            proxy_id=str(pid),
            geo=geo,
            quantity=1,
            period_days=30,
            cost=cost,
            reason=f"прокси {pid} истекает {exp}",
        )
        notifier.send_message(
            f"🔄 <b>Orchestrator: продление прокси</b>\n"
            f"Прокси <b>{pid}</b> (GEO: {geo}) истекает <b>{exp}</b>\n"
            f"Стоимость продления 30 дней: {cost_str}\n\n"
            f"Ответь <b>да {event_id}</b> для продления\n"
            f"или <b>нет {event_id}</b> для отмены"
        )
        sent += 1
        logger.info("[SupplyTracker] Запрос продления прокси %s (GEO: %s)", pid, geo)

    # ── 3. Спайк банов → предложить новый прокси ──────────────────────────────
    ban_count = sp_metrics.get("ban_count", 0)
    if ban_count >= config.PROXY_BAN_SPIKE_THRESH and not expiring and not sent:
        top_platform = sp_metrics.get("top_platform") or "неизвестно"
        event_id = _save_event(
            event_type="purchase_request",
            geo="",
            quantity=1,
            period_days=30,
            cost=None,
            reason=f"бан-спайк: {ban_count} событий за 24ч ({top_platform})",
        )
        notifier.send_message(
            f"🚫 <b>Orchestrator: бан-спайк</b>\n"
            f"Зафиксировано {ban_count} бан-событий за 24ч ({top_platform}).\n"
            f"Рекомендую купить 1 новый прокси для ротации.\n\n"
            f"Ответь <b>да {event_id}</b> для покупки\n"
            f"или <b>нет {event_id}</b> для отмены"
        )
        sent += 1
        logger.info("[SupplyTracker] Запрос покупки прокси (бан-спайк %d)", ban_count)

    return sent


# ─────────────────────────────────────────────────────────────────────────────
# Подтверждение / отклонение
# ─────────────────────────────────────────────────────────────────────────────

def get_pending_purchase() -> Optional[Dict]:
    """Возвращает первое событие, ожидающее подтверждения, или None."""
    with get_db() as conn:
        row = conn.execute("""
            SELECT * FROM proxy_events
            WHERE status = 'awaiting_confirmation'
            ORDER BY created_at ASC LIMIT 1
        """).fetchone()
    return dict(row) if row else None


def confirm_purchase(event_id: int) -> bool:
    """
    Оператор ответил 'да'. Выполняет покупку или продление.
    Возвращает True при успехе.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM proxy_events WHERE id = ? AND status = 'awaiting_confirmation'",
            (event_id,),
        ).fetchone()
    if not row:
        logger.warning("[SupplyTracker] Event #%d не найден или уже обработан", event_id)
        return False

    event  = dict(row)
    e_type = event.get("event_type", "")
    qty    = event.get("quantity") or 1
    bought: List[int] = []

    try:
        if e_type == "renewal_request" and event.get("proxy_id"):
            # Продление конкретных прокси
            ids = [int(x) for x in event["proxy_id"].split(",") if x.strip().isdigit()]
            bought = proxy_manager.renew_proxies(ids, period=event.get("period_days") or 30)
        else:
            # Новая покупка — нужен geoid
            # Если geo не числовой → запрашиваем сначала список ГЕО и ищем соответствие
            # Пока используем упрощённый подход: geo как geoid если числовой
            geo_raw = event.get("geo", "")
            geo_id  = int(geo_raw) if geo_raw and geo_raw.isdigit() else 1
            bought  = proxy_manager.buy_proxy(
                geo_id=geo_id,
                num=qty,
                period=event.get("period_days") or 30,
            )
    except Exception as exc:
        logger.error("[SupplyTracker] confirm_purchase #%d: %s", event_id, exc)

    success    = bool(bought)
    new_status = "executed" if success else "failed"
    _update_event(event_id, new_status, api_response=json.dumps({"bought_ids": bought}))

    if success:
        notifier.send_message(
            f"✅ <b>Прокси {'продлены' if e_type == 'renewal_request' else 'куплены'}:</b> {bought}\n"
            f"Event #{event_id} выполнен."
        )
        notifier.log_notification(
            f"Прокси операция выполнена: {bought}",
            level="info", category="metric",
        )
    else:
        notifier.send_message(f"❌ Операция с прокси не удалась (event #{event_id}). Проверь баланс.")

    return success


def reject_purchase(event_id: int) -> None:
    """Оператор ответил 'нет'."""
    _update_event(event_id, "rejected")
    logger.info("[SupplyTracker] Event #%d отклонён оператором", event_id)
    notifier.send_message(f"🚫 Запрос #{event_id} отменён.")


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────

def _save_event(
    event_type: str,
    geo: str = "",
    proxy_id: str = "",
    operator: str = "",
    quantity: int = 1,
    period_days: int = 30,
    cost: Optional[float] = None,
    reason: str = "",
) -> int:
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO proxy_events
                (event_type, proxy_id, geo, operator, quantity, period_days, cost, reason, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_confirmation')
        """, (event_type, proxy_id, geo, operator, quantity, period_days, cost, reason))
        return cur.lastrowid


def _update_event(event_id: int, status: str, api_response: str = "") -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE proxy_events SET status = ?, api_response = ? WHERE id = ?",
            (status, api_response, event_id),
        )
