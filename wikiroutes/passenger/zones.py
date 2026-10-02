"""Зонирование спроса (§20.2, §31.4, §32.3).

Число зон задаётся формулой ``clamp(округл(pop / 5000), 150, 400)``.
От него зависит бюджет маршрутизации: прогон делается на зону, а не на
точку спроса.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from .params import (
    DemandPoint,
    PassengerOptions,
    ROUTING_WINDOWS,
    ZONE_MAX,
    ZONE_MIN,
    ZONE_POP_PER_ZONE,
    ZONE_ROUND_TO,
    Zone,
)

from .geo import (
    _haversine_m,
)


def passenger_zone_count(population: float) -> int:
    """Число зон по формуле §20.2: ``clamp(округл(pop / 5000), 150, 400)``.

    Округление — до кратного 25, как указано в §20.2 и §31.4. Число зон
    растёт с населением, а не фиксировано: малый город не платит за размер
    большого.
    """
    if not math.isfinite(population) or population <= 0.0:
        return ZONE_MIN
    raw = int(round(population / ZONE_POP_PER_ZONE))
    raw = max(ZONE_MIN, min(ZONE_MAX, raw))
    # Округление вниз до кратного 25: округление вверх подняло бы бюджет T1
    # без единого нового пункта точности.
    rounded = (raw // ZONE_ROUND_TO) * ZONE_ROUND_TO
    return max(ZONE_MIN, min(ZONE_MAX, rounded))


@dataclass(frozen=True, slots=True)
class ZoneSet:
    """Зонирование города: зоны плюс отображение точка → зона."""

    zones: tuple[Zone, ...]
    zone_of: dict[str, int]
    city_population: float
    requested_zone_count: int

    @property
    def count(self) -> int:
        return len(self.zones)

    def router_runs(self) -> int:
        """Число задач T1: зоны × окна маршрутизации (§32.3)."""
        return self.count * len(ROUTING_WINDOWS)

    def max_radius_m(self) -> float:
        """Наибольший радиус зоны — приёмка §32.3 (должен быть < 2 700 м)."""
        return max((zone.radius_m for zone in self.zones), default=0.0)


def _zone_centroid(points: list[DemandPoint]) -> tuple[float, float, float]:
    """Взвешенный по населению центроид группы точек (детерминированно)."""
    total = sum(point.residents + point.jobs for point in points)
    if total <= 0.0:
        lat = sum(point.lat for point in points) / len(points)
        lon = sum(point.lon for point in points) / len(points)
        return lat, lon, 0.0
    lat = sum(point.lat * (point.residents + point.jobs) for point in points) / total
    lon = sum(point.lon * (point.residents + point.jobs) for point in points) / total
    return lat, lon, total


def build_zones(
    points: list[DemandPoint],
    options: PassengerOptions | None = None,
) -> ZoneSet:
    """Кластеризует точки спроса в зоны по соседству (§32.3, шаг 1).

    Кластеризация детерминирована (§32.5): точки обрабатываются в каноническом
    порядке по ключу, границы зон — квадраты постоянного размера в градусах,
    а не результат итеративного алгоритма с плавающим порядком обхода.

    Одна ось детерминизма важнее прочих: **две игры одного города не могут
    получить разное зонирование** (§31.4), иначе скимы несопоставимы.
    """
    opts = options or PassengerOptions()
    ordered = sorted(points, key=lambda point: str(point.key))
    population = sum(point.residents for point in ordered)
    requested = passenger_zone_count(population)

    if not ordered:
        return ZoneSet(zones=(), zone_of={}, city_population=0.0,
                       requested_zone_count=requested)

    if opts.zone_count is not None:
        target = max(1, int(opts.zone_count))
    else:
        target = min(requested, len(ordered))

    # Размер ячейки подбирается так, чтобы занятых ячеек было не больше
    # целевого числа зон: это одна детерминированная процедура вместо
    # итеративного слияния кластеров.
    cell = _zone_cell_size(ordered, target)

    buckets: dict[tuple[int, int], list[DemandPoint]] = {}
    for point in ordered:
        key = (
            math.floor(point.lat / cell),
            math.floor(point.lon / cell),
        )
        buckets.setdefault(key, []).append(point)

    zones: list[Zone] = []
    zone_of: dict[str, int] = {}
    # Канонический порядок зон: по координате ячейки, не по порядку вставки.
    for index, cell_key in enumerate(sorted(buckets)):
        members = sorted(buckets[cell_key], key=lambda point: str(point.key))
        lat, lon, _mass = _zone_centroid(members)
        radius = max(
            _haversine_m(lat, lon, member.lat, member.lon) for member in members
        )
        zones.append(
            Zone(
                index=index,
                lat=lat,
                lon=lon,
                keys=tuple(str(member.key) for member in members),
                residents=sum(member.residents for member in members),
                jobs=sum(member.jobs for member in members),
                radius_m=radius,
            )
        )
        for member in members:
            zone_of[str(member.key)] = index

    return ZoneSet(
        zones=tuple(zones),
        zone_of=zone_of,
        city_population=population,
        requested_zone_count=requested,
    )


def _zone_cell_size(points: list[DemandPoint], target: int) -> float:
    """Подбирает размер ячейки (в градусах) под целевое число зон.

    Бинарный поиск по размеру: занятых ячеек тем меньше, чем крупнее ячейка.
    Возвращает наименьший размер, при котором занятых ячеек не больше цели.
    """
    if target >= len(points):
        return 1e-9  # каждая точка в своей зоне

    def occupied(size: float) -> int:
        seen: set[tuple[int, int]] = set()
        for point in points:
            seen.add(
                (
                    math.floor(point.lat / size),
                    math.floor(point.lon / size),
                )
            )
            if len(seen) > target:
                # Досрочный выход: результат уже превышает цель.
                return len(seen)
        return len(seen)

    low, high = 1e-7, 2.0
    for _ in range(40):
        mid = (low + high) / 2.0
        if occupied(mid) > target:
            low = mid
        else:
            high = mid
    return high
