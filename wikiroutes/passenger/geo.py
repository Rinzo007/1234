"""Геодезия и доступ к полям объектов маршрутов.

Расстояния и чтение полей остановок/маршрутов через duck-typing:
модуль работает и с объектами ``wikiroutes.models``, и со словарями.
"""

from __future__ import annotations

import math
from typing import Any


# ── Мелкие функции ────────────────────────────────────────────────────

try:  # пакет импортирован как каталог (sys.path содержит wikiroutes/)
    from geometry import haversine_km as _haversine_km
except ImportError:  # pragma: no cover — обычный импорт пакета
    try:
        from .geometry import haversine_km as _haversine_km
    except ImportError:
        _haversine_km = None  # type: ignore[assignment]

_EARTH_RADIUS_M: float = 6_371_000.0

#: Километров в градусе широты — приближение для равнинных городов.
#: Для индекса и внутрипарных расстояний точность ~0,1 % не нужна, а
#: гаверсинус на миллионах пар занимает половину времени расчёта.
_KM_PER_DEG_LAT: float = 111.32


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние по гаверсинусу в метрах (переиспользует ``geometry``)."""
    if _haversine_km is not None:
        try:
            return float(_haversine_km(lat1, lon1, lat2, lon2)) * 1000.0
        except ValueError:
            return 0.0
    radius_m = _EARTH_RADIUS_M
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(
        dlambda / 2.0
    ) ** 2
    return 2.0 * radius_m * math.asin(min(1.0, max(0.0, a) ** 0.5))


def _stop_lat(stop: Any) -> float | None:
    raw = stop.get("latitude") if isinstance(stop, dict) else getattr(stop, "latitude", None)
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or not -90.0 <= value <= 90.0:
        return None
    return value


def _stop_lon(stop: Any) -> float | None:
    raw = stop.get("longitude") if isinstance(stop, dict) else getattr(stop, "longitude", None)
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or not -180.0 <= value <= 180.0:
        return None
    return value


def _stop_name(stop: Any) -> str:
    if isinstance(stop, dict):
        return str(stop.get("name") or "").strip()
    return str(getattr(stop, "name", "") or "").strip()


def _stop_id(stop: Any) -> Any:
    if isinstance(stop, dict):
        return stop.get("id")
    return getattr(stop, "id", None)


def _route_type_value(route: Any) -> str:
    raw = getattr(route, "route_type", "")
    return str(getattr(raw, "value", raw) or "").strip().lower()
