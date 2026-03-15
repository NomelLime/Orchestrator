"""
modules/timezone_mapper.py — Конвертация времени публикации по часовым поясам.

Используется config_enforcer.py при применении schedule-изменений.
LLM даёт prime-time для target_geo ("UA" → 20:00 местное → UTC+2 → 18:00 UTC).

Экспорт:
    local_to_utc(time_str, geo) → str   конвертирует "HH:MM" местное → UTC
    utc_to_local(time_str, geo) → str   обратная конвертация
    geo_utc_offset(geo)         → int   смещение в часах (например +3 для RU)
    convert_schedule(times, geo) → List[str]  конвертирует список расписания

Статический словарь — не требует pytz / dateutil.
"""
from __future__ import annotations

from typing import List

# ── Смещения UTC для каждого ISO-2 кода (целые часы, летнее время усреднено) ──

_GEO_UTC_OFFSET: dict[str, int] = {
    # СНГ
    "UA": 2,    "RU": 3,    "KZ": 5,    "BY": 3,
    "UZ": 5,    "AZ": 4,    "AM": 4,    "GE": 4,
    "TJ": 5,    "KG": 6,    "TM": 5,    "MD": 2,

    # Европа
    "PL": 1,    "CZ": 1,    "SK": 1,    "HU": 1,
    "RO": 2,    "BG": 2,    "HR": 1,    "SI": 1,
    "RS": 1,    "BA": 1,    "ME": 1,    "MK": 1,
    "AL": 1,    "DE": 1,    "AT": 1,    "CH": 1,
    "BE": 1,    "NL": 1,    "FR": 1,    "LU": 1,
    "ES": 1,    "IT": 1,    "PT": 0,    "GB": 0,
    "IE": 0,    "DK": 1,    "SE": 1,    "NO": 1,
    "FI": 2,    "EE": 2,    "LV": 2,    "LT": 2,
    "GR": 2,    "TR": 3,

    # Азия
    "CN": 8,    "JP": 9,    "KR": 9,    "IN": 5,    # IN UTC+5:30 → округляем до 5
    "PK": 5,    "BD": 6,    "TH": 7,    "VN": 7,
    "ID": 7,    "MY": 8,    "SG": 8,    "PH": 8,
    "TW": 8,    "HK": 8,    "MM": 6,    "KH": 7,
    "LA": 7,    "NP": 5,    "MN": 8,    "AF": 4,

    # Ближний Восток / Африка
    "SA": 3,    "AE": 4,    "IL": 2,    "IR": 3,
    "IQ": 3,    "EG": 2,    "NG": 1,    "ZA": 2,
    "ET": 3,    "KE": 3,    "TZ": 3,    "GH": 0,
    "SN": 0,    "MA": 0,    "TN": 1,    "DZ": 1,

    # Америка
    "US": -5,   "CA": -5,   "MX": -6,   "BR": -3,
    "AR": -3,   "CL": -4,   "CO": -5,   "PE": -5,
    "VE": -4,   "EC": -5,   "BO": -4,   "PY": -4,
    "UY": -3,   "GT": -6,   "CU": -5,   "DO": -4,

    # Австралия / Океания
    "AU": 10,   "NZ": 12,   "FJ": 12,

    # Специальные / неизвестные
    "XX": 0,
}

_DEFAULT_OFFSET = 0  # UTC если geo не найден


def geo_utc_offset(geo: str) -> int:
    """Возвращает UTC-смещение в часах для данного ISO-2 кода."""
    return _GEO_UTC_OFFSET.get(geo.upper(), _DEFAULT_OFFSET)


def local_to_utc(time_str: str, geo: str) -> str:
    """
    Конвертирует "HH:MM" (местное время) в UTC.
    Например: local_to_utc("20:00", "UA") → "18:00" (UTC+2)
    """
    return _shift_time(time_str, -geo_utc_offset(geo))


def utc_to_local(time_str: str, geo: str) -> str:
    """
    Конвертирует "HH:MM" UTC → местное время.
    Например: utc_to_local("18:00", "UA") → "20:00"
    """
    return _shift_time(time_str, geo_utc_offset(geo))


def convert_schedule(times: List[str], geo: str) -> List[str]:
    """
    Конвертирует список расписания ["20:00", "22:00"] из местного времени geo → UTC.
    Сортирует результат.
    """
    if not times or not geo:
        return times

    converted = [local_to_utc(t, geo) for t in times]
    return sorted(set(converted))


# ── Внутренние утилиты ────────────────────────────────────────────────────────

def _shift_time(time_str: str, delta_hours: int) -> str:
    """Сдвигает "HH:MM" на delta_hours с переходом через полночь."""
    try:
        parts = time_str.strip().split(":")
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return time_str  # некорректный формат — возвращаем как есть

    h = (h + delta_hours) % 24
    if h < 0:
        h += 24
    return f"{h:02d}:{m:02d}"
