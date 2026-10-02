"""Фильтрация маршрутов по названию, bbox, радиусу, кривизне и длине.

Фильтры применяются на уровне направлений: из маршрута удаляются только
те направления, которые не проходят ограничения. Маршрут исключается
целиком, лишь когда после фильтрации у него не остаётся ни одного
валидного направления.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from shapely import make_valid
from shapely.errors import ShapelyError
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from .geometry import haversine_km
from .models import BBox, Direction, FilterLimits, RouteData
from .osm.boundary import meters_to_deg_lat
from .support import route_name_excluded

__all__ = [
    "apply_route_limits",
    "compute_bbox",
    "direction_exceeds_radius",
    "route_exceeds_radius",
]

# Допуск сравнения длин (1 мм): сумма гаверсинусов накапливает погрешность,
# и маршрут «ровно limit» без допуска мог быть отрезан значением 20.000000019.
_LENGTH_EPS_KM = 1e-6

def _iter_route_coords(routes: Iterable[RouteData]):
    """Перечисляет координаты (lat, lon) всех направлений маршрутов."""
    for route in routes:
        for direction in route.directions:
            yield from direction.coords


def compute_bbox(
    routes: Iterable[RouteData],
    buffer_deg: float | None = None,
) -> BBox | None:
    """Вычисляет bounding box по всем координатам маршрутов.

    По умолчанию bbox ровно по границам координат; ``buffer_deg`` добавляет
    отступ в градусах.

    Возвращает ``None``, если не найдено ни одной валидной координаты.

    ИСПРАВЛЕНО (L-01): результирующие координаты clamped в валидные диапазоны
    (lat ∈ [-90, 90], lon ∈ [-180, 180]), чтобы буфер не вывел bbox за пределы
    географической системы координат.
    """
    min_lat = math.inf
    min_lon = math.inf
    max_lat = -math.inf
    max_lon = -math.inf
    found = False

    for lat, lon in _iter_route_coords(routes):
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        found = True
        min_lat = min(min_lat, lat)
        max_lat = max(max_lat, lat)
        min_lon = min(min_lon, lon)
        max_lon = max(max_lon, lon)

    if not found:
        return None

    buffer_deg = max(0.0, float(buffer_deg)) if buffer_deg is not None else 0.0

    # ИСПРАВЛЕНО (L-01): clamp в валидные географические диапазоны.
    return BBox(
        min_lat=max(-90.0, min_lat - buffer_deg),
        min_lon=max(-180.0, min_lon - buffer_deg),
        max_lat=min(90.0, max_lat + buffer_deg),
        max_lon=min(180.0, max_lon + buffer_deg),
    )


def _coords_exceed_radius(
    coords: Sequence[tuple[float, float]],
    center_lat: float,
    center_lon: float,
    radius_km: float,
) -> bool:
    """Проверяет, выходит ли набор координат за пределы круга радиуса.

    Алгоритм оптимизирован:
    1. Если все углы bbox координат внутри радиуса → точно внутри.
    2. Если ближайшая точка bbox (проекция центра) вне радиуса → точно вне.
    3. Иначе — перебор всех точек (fallback).
    """
    if not coords:
        return False

    lats = [lat for lat, _ in coords]
    lons = [lon for _, lon in coords]

    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)

    corners = [
        (min_lat, min_lon),
        (min_lat, max_lon),
        (max_lat, min_lon),
        (max_lat, max_lon),
    ]

    corner_distances = [
        haversine_km(center_lat, center_lon, lat, lon) for lat, lon in corners
    ]

    # Оптимизация: если все углы внутри радиуса, точно внутри.
    if max(corner_distances) <= radius_km:
        return False

    # Оптимизация: если ближайшая точка bbox вне радиуса, точно вне.
    closest_lat = max(min_lat, min(center_lat, max_lat))
    closest_lon = max(min_lon, min(center_lon, max_lon))

    if haversine_km(center_lat, center_lon, closest_lat, closest_lon) > radius_km:
        return True

    # Fallback: перебор всех точек.
    return any(
        haversine_km(center_lat, center_lon, lat, lon) > radius_km
        for lat, lon in coords
    )


def direction_exceeds_radius(
    direction: Direction,
    center_lat: float,
    center_lon: float,
    radius_km: float,
) -> bool:
    """Проверяет, выходит ли направление за пределы круга заданного радиуса."""
    return _coords_exceed_radius(direction.coords, center_lat, center_lon, radius_km)


def direction_outside_boundary(
    direction: Direction,
    boundary_geom=None,
) -> bool:
    """Проверяет, выходит ли направление за пределы границы города.

    ``boundary_geom`` — геометрия границы в координатах ``(lon, lat)``.

    Направление отсеивается, только если хотя бы одна из двух конечных
    точек маршрута (первая/последняя координата) лежит за границей.
    Если обе конечные внутри — направление сохраняется, даже когда
    промежуточная часть пути выходит за границу.

    Возвращает ``False`` для направлений без геометрии, при отсутствии
    границы или при любой ошибке проверки — такие направления не
    отсеиваются по границе.
    """
    if boundary_geom is None:
        return False
    if len(direction.coords) < 1:
        return False
    first = direction.coords[0]
    last = direction.coords[-1]
    try:
        return not (
            boundary_geom.covers(Point(last[1], last[0]))
            and boundary_geom.covers(Point(first[1], first[0]))
        )
    except (TypeError, ValueError, ShapelyError):
        return False


def route_exceeds_radius(
    route: RouteData,
    center_lat: float,
    center_lon: float,
    radius_km: float,
) -> bool:
    """Проверяет, выходит ли маршрут за пределы круга заданного радиуса.

    Возвращает ``False`` для маршрутов без координат.
    """
    coords = [coord for direction in route.directions for coord in direction.coords]
    return _coords_exceed_radius(coords, center_lat, center_lon, radius_km)


@dataclass(slots=True)
class _BoundaryFilter:
    """Граница города: исходная геометрия для измерения доли длины."""

    geom: Any | None = None


@dataclass(slots=True)
class _FilterLog:
    """Накопители удалений направлений за весь прогон."""

    counts: dict[str, int]
    removed: list[tuple[RouteData, Direction, str]]


def _fails_curvilinearity(direction: Direction, limits: FilterLimits) -> bool:
    return limits.curvilinearity > 0 and (
        direction.curvilinearity > limits.curvilinearity
    )


def _fails_min_length(direction: Direction, limits: FilterLimits) -> bool:
    return limits.min_length_km > 0 and (
        direction.km < limits.min_length_km - _LENGTH_EPS_KM
    )


def _fails_max_length(direction: Direction, limits: FilterLimits) -> bool:
    return limits.max_length_km > 0 and (
        direction.km > limits.max_length_km + _LENGTH_EPS_KM
    )


def _fails_radius(direction: Direction, limits: FilterLimits) -> bool:
    return (
        limits.radius_km > 0
        and limits.center_lat is not None
        and limits.center_lon is not None
        and direction_exceeds_radius(
            direction,
            limits.center_lat,
            limits.center_lon,
            limits.radius_km,
        )
    )


def _fails_boundary(direction: Direction, boundary: _BoundaryFilter | None) -> bool:
    return boundary is not None and direction_outside_boundary(
        direction, boundary.geom
    )


# Типы транспорта, для которых фильтр границы города не применяется.
# (удалено по требованию: фильтр действует на все типы)



def _direction_exclusion_reason(
    direction: Direction,
    limits: FilterLimits,
    boundary: _BoundaryFilter | None = None,
) -> str | None:
    """Возвращает причину исключения направления или ``None``, если оно проходит.

    Порядок проверок совпадает с прежней фильтрацией маршрута целиком:
    криволинейность, затем длина (min/max), затем радиус, затем граница
    города. Границы длины включительны с допуском 1 мм (см. ``_LENGTH_EPS_KM``).

    ``boundary`` — подготовленная граница (см. ``_prepare_boundary``); если
    ``None``, фильтр по границе не применяется.
    """
    if _fails_curvilinearity(direction, limits):
        return "криволинейность"
    if _fails_min_length(direction, limits) or _fails_max_length(direction, limits):
        return "длина"
    if _fails_radius(direction, limits):
        return "радиус"
    if _fails_boundary(direction, boundary):
        return "граница"
    return None


def _prepare_boundary(
    boundary_geom: BaseGeometry | None,
    boundary_buffer_m: float,
) -> _BoundaryFilter | None:
    """Подготавливает границу (valid + буфер); ``None``, если границы нет."""
    if boundary_geom is None or boundary_geom.is_empty:
        return None
    # Границы из OSM (Nominatim) нередко невалидны (самопересечения).
    geom = make_valid(boundary_geom)
    if boundary_buffer_m > 0:
        geom = make_valid(geom.buffer(meters_to_deg_lat(boundary_buffer_m)))
    return _BoundaryFilter(geom=geom)


def _filter_route_directions(
    route: RouteData,
    limits: FilterLimits,
    boundary: _BoundaryFilter | None,
    log: _FilterLog,
) -> tuple[RouteData | None, str | None]:
    """Фильтрует направления маршрута; возвращает ``(kept_route, reason)``."""
    # Маршруты с OZON в названии отбрасываются
    if "OZON" in route.name.upper():
        return None, "OZON"
    # Маршруты со словом «детская» в названии отбрасываются
    if route_name_excluded(route.name):
        return None, "детская"
    kept_directions: list[Direction] = []
    removed_reasons: list[str] = []

    for direction in route.directions:
        reason = _direction_exclusion_reason(direction, limits, boundary)
        if reason is None:
            kept_directions.append(direction)
        else:
            log.counts[reason] += 1
            removed_reasons.append(reason)
            log.removed.append((route, direction, reason))

    if not kept_directions:
        # Все направления отфильтрованы — исключаем маршрут целиком.
        reason = removed_reasons[0] if removed_reasons else "нет направлений"
        return None, reason

    if len(kept_directions) == len(route.directions):
        return route, None
    return replace(route, directions=tuple(kept_directions)), None


def apply_route_limits(
    routes: Sequence[RouteData],
    limits: FilterLimits,
    boundary_geom: BaseGeometry | None = None,
    boundary_buffer_m: float = 0.0,
) -> tuple[
    list[RouteData],
    list[tuple[RouteData, str]],
    dict[str, int],
    list[tuple[RouteData, Direction, str]],
]:
    """Применяет фильтры по кривизне, длине, радиусу и границе города.

    Из каждого маршрута удаляются только те направления, которые не
    проходят ограничения. Маршрут сохраняется, если после фильтрации у
    него осталось хотя бы одно направление (с обновлённым набором
    направлений). Маршрут исключается целиком, лишь когда все его
    направления отфильтрованы.

    Возвращает кортеж ``(kept, excluded, direction_counts, removed_directions)``:
    * ``kept`` — маршруты с отфильтрованными направлениями;
    * ``excluded`` — пары ``(route, reason)`` для маршрутов, потерявших
      все направления (причина — первая по порядку проверок);
    * ``direction_counts`` — число удалённых направлений по каждой причине
      (ключи: ``"криволинейность"``, ``"длина"``, ``"радиус"``, ``"граница"``);
    * ``removed_directions`` — пары ``(route, direction, reason)`` для
      каждого удалённого направления (включая направления маршрутов,
      исключённых целиком).

    Фильтр по радиусу применяется только если ``radius_km > 0`` и оба
    центра (``center_lat``, ``center_lon``) заданы. Фильтр по границе
    применяется, только если передана ``boundary_geom`` (полигон в
    координатах ``(lon, lat)``); при ``boundary_buffer_m > 0`` граница
    предварительно расширяется на указанное число метров.
    """
    boundary = _prepare_boundary(boundary_geom, boundary_buffer_m)

    kept: list[RouteData] = []
    excluded: list[tuple[RouteData, str]] = []
    direction_counts: dict[str, int] = {
        "криволинейность": 0,
        "длина": 0,
        "радиус": 0,
        "граница": 0,
    }
    removed_directions: list[tuple[RouteData, Direction, str]] = []
    log = _FilterLog(counts=direction_counts, removed=removed_directions)

    for route in routes:
        kept_route, reason = _filter_route_directions(route, limits, boundary, log)
        if kept_route is None:
            excluded.append((route, reason))
        else:
            kept.append(kept_route)

    return kept, excluded, log.counts, log.removed
