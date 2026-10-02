from collections.abc import Mapping
from typing import Any

from .decoding import unshift_coords
from .models import Direction, ParsedRoute, Stop


def _parse_trip_direction(trip: Mapping[str, Any]) -> Direction | None:
    """Собирает направление из одного trip (геометрия + остановки)."""
    line = trip.get("line") or {}
    if not isinstance(line, Mapping):
        return None

    coords = unshift_coords(line.get("coordinates"))
    if not coords:
        return None

    stops: list[Stop] = []
    raw_stops = line.get("stops") or []
    if isinstance(raw_stops, list):
        for stop_index, item in enumerate(raw_stops):
            if not isinstance(item, Mapping):
                continue
            stop = Stop.from_api(dict(item), shift_index=stop_index)
            if stop is not None:
                stops.append(stop)

    name = ""
    if stops:
        first = stops[0].name or "?"
        last = stops[-1].name or "?"
        name = f"{first} → {last}"

    return Direction(coords=coords, stops=tuple(stops), name=name)


def parse_route_payload(payload: Any) -> ParsedRoute | None:
    """Преобразует декодированный payload WikiRoutes в доменную модель маршрута.

    Повторяющиеся геометрии направлений удаляются по равенству кортежей
    координат (хэш кортежа вместо дорогой SHA-256 по строке).
    Некорректные записи и маршруты без валидной геометрии пропускаются.
    """
    if not isinstance(payload, Mapping):
        return None

    trips = payload.get("trips") or []
    if not isinstance(trips, list):
        trips = []

    directions: list[Direction] = []
    seen: set[tuple[Any, ...]] = set()

    for trip in trips:
        if not isinstance(trip, Mapping):
            continue

        direction = _parse_trip_direction(trip)
        if direction is None:
            continue

        if direction.coords in seen:
            continue
        seen.add(direction.coords)
        directions.append(direction)

    if not directions:
        return None

    return ParsedRoute(
        directions=tuple(directions),
        price=str(payload.get("price") or ""),
        company=str(payload.get("company") or ""),
        active=bool(payload.get("active", True)),
        transport_class=str(payload.get("transport_class") or ""),
    )
