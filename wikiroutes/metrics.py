"""Типизированные результаты GIS-расчётов: POI и Overture."""

from dataclasses import dataclass, field

__all__ = [
    "ADDITIVE_METRICS",
    "NON_ADDITIVE_METRICS",
    "OvertureStats",
    "PoiStats",
]

# Метрики, которые можно суммировать для независимых результатов.
ADDITIVE_METRICS = frozenset(
    {
        "total_area_m2",
        "total_value",
    }
)

# Метрики, которые зависят от конкретной геометрии/набора объектов
# и должны пересчитываться после изменения набора маршрутов.
NON_ADDITIVE_METRICS = frozenset(
    {
        "corridor_m2",
        "tiles_used",
        "count",
        "by_type",
    }
)


@dataclass(slots=True)
class PoiStats:
    """Результат расчёта ценности POI вдоль маршрута."""

    total_value: float = 0.0
    count: int = 0


@dataclass(slots=True)
class OvertureStats:
    """Результат расчёта площади объектов Overture."""

    total_area_m2: float = 0.0
    corridor_m2: float = 0.0
    count: int = 0
    ok: bool = False
