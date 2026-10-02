"""Сокращение маршрутов до одного направления с наибольшим количеством POI.

Требование продукта: у одного маршрута должно быть ровно одно направление.
Из нескольких направлений (обычно «туда»/«обратно») оставляется то, у
которого больше всего POI (poi_stops). При отсутствии POI-данных или при
равенстве счётчика оставляется первое направление (``di == 0``).

Схлопывание выполняется сразу после стадии обогащения, поэтому дальше по
конвейеру (дедупликация, spatial, экспорт) маршруты уже одненаправленные.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..models import RouteData

__all__ = ["collapse_routes_to_single_direction"]


def _direction_poi_from_stats(
    route_id: int,
    di: int,
    poi_stops_dir_stats: dict[tuple[int, int], Any],
) -> int:
    """Количество POI одного направления (0, если статистики нет)."""
    stat = poi_stops_dir_stats.get((route_id, di))
    if stat is None:
        return 0
    return int(getattr(stat, "count", 0) or 0)


def _best_direction_index(
    route: RouteData,
    poi_stops_dir_stats: dict[tuple[int, int], Any],
) -> int:
    """Индекс направления с максимальным числом POI.

    При равенстве или отсутствии POI побеждает первое направление (``di == 0``).
    """
    return max(
        range(len(route.directions)),
        key=lambda di: (
            _direction_poi_from_stats(route.route_id, di, poi_stops_dir_stats),
            -di,
        ),
    )


def collapse_routes_to_single_direction(
    routes: list[RouteData],
    poi_stops_dir_stats: dict[tuple[int, int], Any] | None,
    poi_stops_stats: dict[int, Any] | None,
) -> tuple[list[RouteData], dict[tuple[int, int], Any], dict[int, Any]]:
    """Сводит каждый маршрут к одному направлению с наибольшим количеством POI.

    Возвращает ``(routes, dir_stats, stats)`` с перепривязанными статистиками:
    выжившее направление всегда индексируется как ``di == 0``, маршрутная
    статистика равна статистике выжившего направления.
    """
    stats = poi_stops_stats or {}
    dir_stats = poi_stops_dir_stats or {}

    new_routes: list[RouteData] = []
    new_dir_stats: dict[tuple[int, int], Any] = {}
    new_stats: dict[int, Any] = {}

    for route in routes:
        if route.error or not route.directions:
            new_routes.append(route)
            continue

        if len(route.directions) <= 1:
            kept_route = route
            best_di = 0
        else:
            best_di = _best_direction_index(route, dir_stats)
            kept_route = replace(route, directions=(route.directions[best_di],))

        new_routes.append(kept_route)

        stat = dir_stats.get((route.route_id, best_di))
        if stat is not None:
            new_dir_stats[(route.route_id, 0)] = stat
            new_stats[route.route_id] = stat

    return new_routes, new_dir_stats, new_stats