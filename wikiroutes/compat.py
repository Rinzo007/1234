"""Совместимый доступ к полям остановок (dict-подобные и объектные остановки).

Модуль обеспечивает единообразный доступ к полям остановки (id, name,
latitude, longitude) независимо от того, представлена ли остановка как
отображение (Mapping/dict) или как объект с атрибутами. Используется в
``support.py``, ``dedup.py`` и других модулях.

Парсинг ``id`` согласован с ``models.Stop.from_api``: оба обрабатывают одни
и те же данные остановок и обязаны возвращать одинаковый результат.
"""

import math
from collections.abc import Mapping
from typing import Any

__all__ = [
    "stop_id",
    "stop_lat",
    "stop_lon",
    "stop_name",
]


def _as_float(value: Any) -> float | None:
    """Приводит значение к ``float``; возвращает ``None`` для некорректных.

    ИСПРАВЛЕНО (L-02): ``bool`` отклоняется — True/False не являются
    координатами (``float(True)`` иначе давал бы ``1.0``).

    ИСПРАВЛЕНО (L-01, L-03): ``math.isfinite`` отклоняет и ``NaN``, и ``±inf``
    (ранее только ``NaN`` через неочевидную идиому ``result != result``).
    """
    # bool — подкласс int, но не является корректной координатой.
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def stop_name(stop: Any) -> str:
    """Возвращает название остановки (пустую строку, если его нет).

    ИСПРАВЛЕНО (L-04): добавлен ``strip`` для единообразия с
    ``models.Stop.from_api``.
    """
    if isinstance(stop, Mapping):
        return str(stop.get("name") or "").strip()
    return str(getattr(stop, "name", "") or "").strip()


def stop_id(stop: Any) -> int | None:
    """Возвращает целочисленный id остановки или ``None``.

    ИСПРАВЛЕНО (M-01): логика приведена в соответствие с
    ``models.Stop.from_api``:

    - ``bool`` отклоняется (``isinstance(True, int)`` истинно, и ранее
      ``True`` возвращался как id == 1);
    - целочисленные ``float`` (напр. ``123.0`` из JSON) приводятся к ``int``;
    - строковые id очищаются от пробелов перед ``isdigit()``.
    """
    raw = stop.get("id") if isinstance(stop, Mapping) else getattr(stop, "id", None)
    if raw is None:
        return None
    # bool — подкласс int: явно отклоняем, чтобы не принять True/False за id.
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    # Целочисленный float (например, 123.0 из JSON) → int.
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str):
        stripped = raw.strip()
        # isascii() отсекает unicode-цифры вроде "²" (isdigit()==True,
        # но int("²") бросает ValueError).
        if stripped.isascii() and stripped.isdigit():
            return int(stripped)
    return None


def stop_lat(stop: Any) -> float | None:
    """Возвращает широту остановки или ``None``."""
    if isinstance(stop, Mapping):
        return _as_float(stop.get("latitude"))
    return _as_float(getattr(stop, "latitude", None))


def stop_lon(stop: Any) -> float | None:
    """Возвращает долготу остановки или ``None``."""
    if isinstance(stop, Mapping):
        return _as_float(stop.get("longitude"))
    return _as_float(getattr(stop, "longitude", None))
