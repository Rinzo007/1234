"""Сокращение маршрутов до одного направления с наибольшим количеством POI."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wikiroutes.enums import RouteType
from wikiroutes.metrics import PoiStats
from wikiroutes.models import Direction, RouteData
from wikiroutes.pipeline.single_direction import collapse_routes_to_single_direction


def _route(route_id: int, direction_names: tuple[str, ...]) -> RouteData:
    directions = tuple(
        Direction(
            coords=((0.0, 0.0), (1.0, 1.0)),
            stops=(),
            name=name,
        )
        for name in direction_names
    )
    return RouteData(
        name=f"route-{route_id}",
        route_type=RouteType.BUS,
        route_id=route_id,
        url="",
        directions=directions,
    )


def test_keeps_direction_with_most_poi():
    route = _route(1, ("туда", "обратно"))
    dir_stats = {
        (1, 0): PoiStats(count=3),
        (1, 1): PoiStats(count=7),
    }
    routes, new_dir_stats, new_stats = collapse_routes_to_single_direction(
        [route], dir_stats, {1: PoiStats(count=10)}
    )
    assert len(routes) == 1
    assert len(routes[0].directions) == 1
    assert routes[0].directions[0].name == "обратно"
    # Выжившее направление всегда переиндексируется как di == 0.
    assert new_dir_stats == {(1, 0): PoiStats(count=7)}
    assert new_stats[1].count == 7


def test_tie_keeps_first_direction():
    route = _route(2, ("туда", "обратно"))
    dir_stats = {
        (2, 0): PoiStats(count=4),
        (2, 1): PoiStats(count=4),
    }
    routes, new_dir_stats, _ = collapse_routes_to_single_direction(
        [route], dir_stats, {}
    )
    assert len(routes[0].directions) == 1
    assert routes[0].directions[0].name == "туда"
    assert new_dir_stats == {(2, 0): PoiStats(count=4)}


def test_no_stats_falls_back_to_first_direction():
    route = _route(3, ("туда", "обратно"))
    routes, new_dir_stats, _ = collapse_routes_to_single_direction(
        [route], None, None
    )
    assert len(routes[0].directions) == 1
    assert routes[0].directions[0].name == "туда"
    assert new_dir_stats == {}


def test_single_direction_route_unchanged():
    route = _route(4, ("туда",))
    routes, new_dir_stats, _ = collapse_routes_to_single_direction(
        [route], {(4, 0): PoiStats(count=2)}, {4: PoiStats(count=2)}
    )
    assert routes[0].directions == route.directions
    assert new_dir_stats == {(4, 0): PoiStats(count=2)}


def test_missing_stats_for_one_direction_picks_other():
    route = _route(5, ("туда", "обратно"))
    dir_stats = {
        (5, 0): PoiStats(count=0),
        (5, 1): PoiStats(count=5),
    }
    routes, _, _ = collapse_routes_to_single_direction([route], dir_stats, {})
    assert routes[0].directions[0].name == "обратно"


def test_empty_routes_passthrough():
    routes, new_dir_stats, new_stats = collapse_routes_to_single_direction(
        [], {(1, 0): PoiStats(count=1)}, {1: PoiStats(count=1)}
    )
    assert routes == []
    assert new_dir_stats == {}
    assert new_stats == {}