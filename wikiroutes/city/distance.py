"""Геодезические расстояния для проверок города.

Отдельный модуль потому, что расстояние здесь нужно **одно и то же** в двух
местах — в проверке расстояния между точками спроса и в подсчёте радиуса зоны —
и расхождение между ними было бы расхождением выводов при одних данных.

Расстояние считается геодезически на эллипсоиде WGS84, а не в градусах: на
широте 51° градус долготы короче километра почти вдвое, и проверка «точки не
слишком далеко» на градусах прошла бы город, в котором они слишком далеко.
"""

from __future__ import annotations

import math
from typing import Sequence


#: Средний радиус Земли, м. Используется как запасной путь без ``pyproj``.
EARTH_RADIUS_M: float = 6_371_008.8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние между двумя точками, м."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, a)))


def nearest_neighbour_distances_m(
    coordinates: Sequence[tuple[float, float]],
) -> list[float]:
    """Расстояние от каждой точки до ближайшей соседней, м.

    ``coordinates`` — пары ``(долгота, широта)``. Считается только между
    **соседями по сетке**: полный перебор всех пар на 7 000 точках — это
    25 миллионов расстояний ради проверки, которой хватает на соседей.

    Сетка строится с шагом, равным среднему расстоянию между соседями: при таком
    шаге ближайший сосед всегда лежит в одном из девяти соседних узлов, и
    перебор остаётся линейным.
    """
    count = len(coordinates)
    if count < 2:
        return [0.0] * count

    step = _grid_step_degrees(coordinates)
    buckets: dict[tuple[int, int], list[int]] = {}
    for index, (lon, lat) in enumerate(coordinates):
        key = (int(math.floor(lon / step)), int(math.floor(lat / step)))
        buckets.setdefault(key, []).append(index)

    distances: list[float] = []
    for index, (lon, lat) in enumerate(coordinates):
        cell = (int(math.floor(lon / step)), int(math.floor(lat / step)))
        best = math.inf
        # Поиск по расширяющимся кольцам, а не только 3×3.
        #
        # Шаг сетки считается по extent'у точки данных, и при неравномерном
        # распределении (а оно неравномерно по построению: население сгущается
        # в центре) некоторые точки оказываются без соседей в 3×3. Объявлять их
        # бесконечно далёкими нельзя: это значило бы «точка размещена ужасно
        # плохо», а на деле она просто в разрежённом краю. Поэтому кольца
        # расширяются, и бесконечность остаётся только для точки, у которой
        # соседей нет вовсе.
        for ring in range(0, 4):
            for d_x in range(-ring, ring + 1):
                for d_y in range(-ring, ring + 1):
                    # Кольцо 0 — это центральная ячейка, и её нужно проверить
                    # обязательно: при грубом шаге сетки несколько соседних
                    # точек попадают в одну ячейку, и без неё они считаются
                    # отсутствующими.
                    if max(abs(d_x), abs(d_y)) != ring:
                        continue
                    for other in buckets.get((cell[0] + d_x, cell[1] + d_y), ()):
                        if other == index:
                            continue
                        other_lon, other_lat = coordinates[other]
                        distance = haversine_m(lat, lon, other_lat, other_lon)
                        if distance < best:
                            best = distance
            if math.isfinite(best):
                # Первый найденный сосед уже ближайший по кольцу: расширять
                # дальше незачем, расстояние по кольцу убывает.
                break
        distances.append(best if math.isfinite(best) else math.inf)
    return distances


def _grid_step_degrees(coordinates: Sequence[tuple[float, float]]) -> float:
    """Шаг сетки, близкий к среднему расстоянию между соседями.

    Берётся по extent точки данных: при равномерной сетке это примерно
    расстояние между соседями, и девять соседних узлов покрывают нужный круг.
    """
    lons = [value[0] for value in coordinates]
    lats = [value[1] for value in coordinates]
    span_lon = max(lons) - min(lons)
    span_lat = max(lats) - min(lats)
    side = max(math.sqrt(len(coordinates)), 2.0)
    step_lon = span_lon / side if span_lon > 0.0 else 0.0
    step_lat = span_lat / side if span_lat > 0.0 else 0.0
    step = max(step_lon, step_lat)
    return step if step > 0.0 else 0.01


__all__ = [
    "EARTH_RADIUS_M",
    "haversine_m",
    "nearest_neighbour_distances_m",
]
