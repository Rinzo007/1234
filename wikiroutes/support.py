"""Общие вспомогательные функции для форматирования и терминалов."""

import math
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from compat import stop_lat, stop_lon, stop_name

__all__ = [
    "SOURCE_LABELS",
    "TYPE_LABELS",
    "clean_terminal_name",
    "collect_terminal_routes",
    "direction_endpoints",
    "fix_stop_coord",
    "fmt_curv_cell",
    "fmt_curv_str",
    "format_dedup_id",
    "in_bbox",
    "normalize_terminal",
    "round_half_up",
    "route_name_excluded",
    "route_number",
    "route_passes_number_filter",
    "safe_float",
    "source_label",
    "type_label",
]


SOURCE_LABELS: dict[str, str] = {
    "wikiroutes": "WikiRoutes",
    "osm": "OSM",
    "idea": "Идея",
}


TYPE_LABELS: dict[str, str] = {
    "trolleybus": "Троллейбус",
    "tram": "Трамвай",
    "water": "Водный транспорт",
    "bus": "Автобус",
    "minibus": "Автобус",
    "metro": "Метро",
    "train": "Поезд",
    "funicular": "Фуникулёр",
    "cable": "Канатная дорога",
    "monorail": "Монорельс",
    "electrobus": "Электробус",
    "idea": "Идея",
}


def type_label(route_type: object) -> str:
    """Возвращает русскую метку типа транспорта.

    Принимает ``RouteType`` или строковое значение типа транспорта.
    """
    value = getattr(route_type, "value", route_type)
    if isinstance(value, str):
        return TYPE_LABELS.get(value, value)
    return str(route_type)


def source_label(value: object) -> str:
    """Возвращает отображаемую метку источника данных маршрута."""
    key = str(value or "").strip().lower()
    return SOURCE_LABELS.get(key, str(value or ""))


def safe_float(value: object, default: float = 0.0) -> float:
    """Безопасно приводит значение к float; NaN/inf заменяет на ``default``."""
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def round_half_up(value: float, ndigits: int = 0) -> float:
    """Округляет ``value`` от нуля вверх на границе (ROUND_HALF_UP).

    Встроенный ``round`` использует banker's rounding, а для отчётов нужна
    классическая схема (0.25 → 0.3, -0.25 → -0.3).
    """
    if not math.isfinite(value):
        return value
    factor = Decimal(10) ** ndigits
    return float(Decimal(str(value)).quantize(Decimal(1) / factor, rounding=ROUND_HALF_UP))


def format_dedup_id(value: object) -> str:
    """Форматирует идентификатор маршрута/направления для отчётов.

    ``3689`` → ``"3689"``; ключ направления ``(3689, 0)`` → ``"3689/0"``.
    """
    if isinstance(value, tuple) and len(value) == 2:
        return f"{value[0]}/{value[1]}"
    return str(value)


def fmt_curv_cell(value: float | None) -> float | str | None:
    """Форматирует коэффициент криволинейности для ячейки Excel."""
    if value is None:
        return None
    try:
        if math.isinf(value):
            return "∞"
        if math.isnan(value):
            return None
    except (TypeError, ValueError):
        pass
    return round(value, 2)


def fmt_curv_str(value: float | None) -> str:
    """Форматирует коэффициент криволинейности в строку."""
    try:
        if value is None:
            return ""
        if math.isinf(value):
            return "∞"
        if math.isnan(value):
            return ""
    except (TypeError, ValueError):
        pass
    return f"{value:.2f}"


def direction_endpoints(direction: object) -> tuple[str, str]:
    """Возвращает названия первой и последней остановок направления.

    Если список остановок недоступен, пытается разобрать ``direction.name``
    в формате ``«первая → последняя»``.
    """
    if direction is None:
        return "", ""
    stops = getattr(direction, "stops", None)
    if stops:
        first = stop_name(stops[0])
        last = stop_name(stops[-1])
        if first or last:
            return first, last
    name = getattr(direction, "name", "") or ""
    if "→" in name:
        first, last = name.split("→", 1)
        return first.strip(), last.strip()
    return "", ""


def _bbox_object_tuple(bbox: Any) -> tuple[float, float, float, float] | None:
    """Кортеж из bbox-объекта с атрибутами min_/max_. Начальная валидация."""
    if not (hasattr(bbox, "min_lat") and hasattr(bbox, "min_lon")):
        return None
    if not (hasattr(bbox, "max_lat") and hasattr(bbox, "max_lon")):
        return None
    return (bbox.min_lat, bbox.min_lon, bbox.max_lat, bbox.max_lon)


def _bbox_sequence_tuple(bbox: Any) -> tuple[float, float, float, float] | None:
    """Кортеж из последовательности [lat, lon, lat, lon]. Начальная валидация."""
    if not (isinstance(bbox, (tuple, list)) and len(bbox) == 4):
        return None
    try:
        return (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    except (ValueError, TypeError):
        return None


def _bbox_to_tuple(bbox: Any) -> tuple[float, float, float, float] | None:
    """Приводит bbox к кортежу (min_lat, min_lon, max_lat, max_lon)."""
    if bbox is None:
        return None
    as_object = _bbox_object_tuple(bbox)
    if as_object is not None:
        return as_object
    return _bbox_sequence_tuple(bbox)


def in_bbox(
    lat: float | None,
    lon: float | None,
    bbox: Any,
) -> bool:
    """Проверяет, попадает ли точка в bbox (поддерживает объект BBox и кортеж)."""
    if bbox is None:
        return False
    if lat is None or lon is None:
        return False
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    bbox_tuple = _bbox_to_tuple(bbox)
    if bbox_tuple is None:
        return False
    min_lat, min_lon, max_lat, max_lon = bbox_tuple
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def fix_stop_coord(
    stop: object,
    index: int | None,
    bbox: Any,
) -> tuple[float, float] | None:
    """Возвращает корректные координаты остановки.

    Координаты уже приводятся в порядок при разборе payload
    (``Stop.from_api`` с ``shift_index``), поэтому функция только проверяет
    допустимость диапазона и при необходимости попадание в bbox.
    Аргумент ``index`` сохранён для совместимости с вызывающим кодом.
    """
    lat = stop_lat(stop)
    lon = stop_lon(stop)
    if lat is None or lon is None:
        return None

    if in_bbox(lat, lon, bbox):
        return lat, lon

    if bbox is None and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
        return lat, lon
    return None


def normalize_terminal(name: str) -> str:
    """Нормализует название терминала для использования как ключ группировки."""
    if not name:
        return ""
    text = re.sub(r"[«»\"'’`]", "", str(name).strip().lower())
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ,;:!?-.")


def clean_terminal_name(name: str) -> str:
    """Удаляет из названия терминала примечания в скобках, включая вложенные."""
    text = str(name or "")
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\([^()]*\)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def route_number(name: str) -> int | None:
    """Возвращает номер маршрута — первую группу цифр в названии.

    Примеры: ``"11"`` → 11, ``"э11"`` → 11, ``"11а"`` → 11, ``"б/н"`` → None.
    """
    match = re.search(r"\d+", str(name or ""))
    return int(match.group()) if match else None


def route_passes_number_filter(name: str, max_route_number: int) -> bool:
    """True, если маршрут проходит фильтр ``--max-route-number``.

    При активном фильтре исключаются маршруты без номера, с номером больше
    лимита и маршруты со скобками в названии.
    """
    if max_route_number <= 0:
        return True

    if "(" in name or ")" in name:
        return False

    num = route_number(name)
    return num is not None and num <= max_route_number


_ROUTE_NAME_EXCLUDED_SUBSTRINGS: tuple[str, ...] = ("детская",)


def route_name_excluded(name: str) -> bool:
    """True, если название маршрута попадает под исключительный фильтр.

    Исключаются маршруты со словом «детская» в названии (например,
    «Детская железная дорога») — регистр не важен.
    """
    lowered = str(name or "").lower()
    return any(banned in lowered for banned in _ROUTE_NAME_EXCLUDED_SUBSTRINGS)


def _ingest_terminal_stop(
    stop: Any,
    route: Any,
    terminals: dict[str, dict[str, Any]],
    seen: set[str],
) -> None:
    """Добавляет конечную остановку в группу терминала (и в seen)."""
    raw_name = stop_name(stop)
    cleaned_name = clean_terminal_name(raw_name)
    key = normalize_terminal(cleaned_name)
    if not key or key in seen:
        return
    seen.add(key)
    record = terminals.setdefault(key, {"name": cleaned_name, "routes": []})
    record["routes"].append(route)


def collect_terminal_routes(routes: list[Any]) -> dict[str, dict[str, Any]]:
    """Группирует маршруты по нормализованным названиям конечных остановок."""
    terminals: dict[str, dict[str, Any]] = {}

    for route in routes:
        if getattr(route, "error", None) or not getattr(route, "directions", None):
            continue

        seen: set[str] = set()

        for direction in route.directions:
            stops = getattr(direction, "stops", None)
            if not stops:
                continue

            for stop in (stops[0], stops[-1]):
                _ingest_terminal_stop(stop, route, terminals, seen)

    return terminals