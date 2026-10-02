"""Геодезические вычисления для городских транспортных сетей.

Координаты передаются в порядке ``(lat, lon)`` (широта, долгота) в градусах.
Расстояния считаются на сферической модели Земли с диаметром
``EARTH_DIAMETER_KM``. Для эллипсоидальной геодезической точности используйте
``pyproj.Geod`` или ``geographiclib``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

from constants import EARTH_DIAMETER_KM

if TYPE_CHECKING:
    from type_defs import Coordinate

__all__ = [
    "geometry_diameter_km",
    "haversine_km",
    "point_to_segment_dist_km",
    "polyline_km",
]

_EARTH_RADIUS_KM = EARTH_DIAMETER_KM / 2.0
_COORD_EPS = 1e-12


def _validate_point(lat: float, lon: float) -> None:
    """Проверяет, что точка является конечной WGS84-координатой."""
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise ValueError("Координаты должны быть конечными числами")
    if not -90.0 <= lat <= 90.0:
        raise ValueError(f"Широта вне диапазона [-90, 90]: {lat!r}")
    if not -180.0 <= lon <= 180.0:
        raise ValueError(f"Долгота вне диапазона [-180, 180]: {lon!r}")


def _angle_between(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> float:
    """Угол между единичными 3D-векторами в радианах."""
    cross_x = a[1] * b[2] - a[2] * b[1]
    cross_y = a[2] * b[0] - a[0] * b[2]
    cross_z = a[0] * b[1] - a[1] * b[0]
    cross_norm = math.sqrt(cross_x**2 + cross_y**2 + cross_z**2)
    dot = max(-1.0, min(1.0, a[0] * b[0] + a[1] * b[1] + a[2] * b[2]))
    return math.atan2(cross_norm, dot)


def _unit_vector(lat: float, lon: float) -> tuple[float, float, float]:
    """Преобразует WGS84 широту/долготу в единичный 3D-вектор."""
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    cos_lat = math.cos(lat_r)
    return (
        cos_lat * math.cos(lon_r),
        cos_lat * math.sin(lon_r),
        math.sin(lat_r),
    )


def _point_to_great_circle_segment_dist_km(
    plat: float,
    plon: float,
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    """Расстояние от точки до кратчайшей дуги большого круга."""
    _validate_point(plat, plon)
    _validate_point(lat1, lon1)
    _validate_point(lat2, lon2)

    if lat1 == lat2 and lon1 == lon2:
        return haversine_km(plat, plon, lat1, lon1)

    a = _unit_vector(lat1, lon1)
    b = _unit_vector(lat2, lon2)
    p = _unit_vector(plat, plon)

    # Нормаль плоскости большого круга через A и B.
    nx = a[1] * b[2] - a[2] * b[1]
    ny = a[2] * b[0] - a[0] * b[2]
    nz = a[0] * b[1] - a[1] * b[0]
    n_norm = math.sqrt(nx**2 + ny**2 + nz**2)

    # Для почти антиподальных точек кратчайшая дуга не определена однозначно.
    if n_norm <= _COORD_EPS:
        return min(
            haversine_km(plat, plon, lat1, lon1),
            haversine_km(plat, plon, lat2, lon2),
        )

    nx /= n_norm
    ny /= n_norm
    nz /= n_norm

    # Проекция P на плоскость большого круга; нормализация возвращает точку
    # пересечения с единичной сферой. Есть две диаметрально противоположные
    # точки, поэтому проверяем обе и выбираем ту, что лежит на дуге A-B.
    dot_pn = p[0] * nx + p[1] * ny + p[2] * nz
    qx = p[0] - dot_pn * nx
    qy = p[1] - dot_pn * ny
    qz = p[2] - dot_pn * nz
    q_norm = math.sqrt(qx**2 + qy**2 + qz**2)

    endpoint_dist = min(
        haversine_km(plat, plon, lat1, lon1),
        haversine_km(plat, plon, lat2, lon2),
    )

    if q_norm <= _COORD_EPS:
        return endpoint_dist

    qx /= q_norm
    qy /= q_norm
    qz /= q_norm

    candidates = ((qx, qy, qz), (-qx, -qy, -qz))
    arc_ab = _angle_between(a, b)
    best = endpoint_dist
    tolerance = 1e-12

    for candidate in candidates:
        arc_aq = _angle_between(a, candidate)
        arc_qb = _angle_between(candidate, b)
        if arc_aq + arc_qb <= arc_ab + tolerance:
            point_distance = _angle_between(p, candidate) * _EARTH_RADIUS_KM
            best = min(best, point_distance)

    return best


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние между двумя точками по формуле гаверсинусов (км)."""
    lat1 = float(lat1)
    lon1 = float(lon1)
    lat2 = float(lat2)
    lon2 = float(lon2)
    _validate_point(lat1, lon1)
    _validate_point(lat2, lon2)

    if lat1 == lat2 and lon1 == lon2:
        return 0.0

    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    lat1_r = math.radians(lat1)
    lat2_r = math.radians(lat2)

    a = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2.0) ** 2
    )
    a = max(0.0, min(1.0, a))
    return 2.0 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def polyline_km(coords: Sequence[Coordinate]) -> float:
    """Длина ломаной в километрах.

    Возвращает ``0.0`` для последовательности менее чем из двух точек.
    """
    if len(coords) < 2:
        return 0.0
    return sum(
        haversine_km(*coords[index - 1], *coords[index])
        for index in range(1, len(coords))
    )


def geometry_diameter_km(
    coords: Sequence[Coordinate],
    samples: int = 100,
    exact: bool = False,
) -> float:
    """Максимальное расстояние между парой точек геометрии в километрах.

    При ``exact=False`` используется равномерное сэмплирование примерно
    ``samples`` точек. При ``exact=True`` вычисляются все пары O(n²).
    """
    if samples <= 0:
        raise ValueError("samples должен быть положительным")

    count = len(coords)
    if count < 2:
        return 0.0

    if exact or count <= samples:
        best = 0.0
        for i in range(count):
            for j in range(i + 1, count):
                distance = haversine_km(*coords[i], *coords[j])
                best = max(best, distance)
        return best

    step = max(1, count // samples)
    indexes = set(range(0, count, step))
    indexes.add(0)
    indexes.add(count - 1)
    points = [coords[index] for index in sorted(indexes)]

    best = 0.0
    point_count = len(points)

    for i in range(point_count):
        first = points[i]

        for j in range(i + 1, point_count):
            distance = haversine_km(*first, *points[j])

            best = max(best, distance)

    return best


def point_to_segment_dist_km(
    plat: float,
    plon: float,
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    """Расстояние от точки до кратчайшей дуги большого круга в километрах.

    В отличие от прежней градусной проекции, широта/долгота не считаются
    равноправными декартовыми координатами. Расчёт выполняется на сфере и
    корректен как для коротких городских сегментов, так и для длинных дуг.
    """
    return _point_to_great_circle_segment_dist_km(
        float(plat),
        float(plon),
        float(lat1),
        float(lon1),
        float(lat2),
        float(lon2),
    )
