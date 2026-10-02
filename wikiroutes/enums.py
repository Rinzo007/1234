"""Перечисления типов транспорта и форматов экспорта."""

from enum import StrEnum

from support import TYPE_LABELS, type_label

__all__ = [
    "TYPE_LABELS",
    "ExportFormat",
    "RouteType",
    "parse_route_type",
    "type_label",
]


class RouteType(StrEnum):
    """Поддерживаемые типы общественного транспорта."""

    TROLLEYBUS = "trolleybus"
    TRAM = "tram"
    WATER = "water"
    BUS = "bus"

    METRO = "metro"
    TRAIN = "train"
    FUNICULAR = "funicular"
    CABLE = "cable"
    MONORAIL = "monorail"
    ELECTROBUS = "electrobus"
    IDEA = "idea"


class ExportFormat(StrEnum):
    """Поддерживаемые форматы экспортируемых результатов."""

    XLSX = "xlsx"
    KML = "kml"


def parse_route_type(value: object) -> RouteType:
    """Разбирает тип маршрута из строки или ``RouteType``.

    Устаревшее значение ``"minibus"`` объединено с ``"bus"``.
    """
    if isinstance(value, RouteType):
        return value
    normalized = str(value).strip().lower()
    if normalized == "minibus":
        normalized = "bus"
    return RouteType(normalized)
