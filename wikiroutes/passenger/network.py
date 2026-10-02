"""Транспортная сеть и поиск пути (§6.2, §6.3, §8.2).

Частотная модель: расписание линии хранится по четырём окнам
маршрутизации, путь ищется одним прогоном Дейкстры от начала.
"""

from __future__ import annotations

from dataclasses import dataclass
import heapq
import math
from typing import Any

from .geo import _KM_PER_DEG_LAT
from .params import (
    passenger_validate_headway,
    _BUS_LIKE,
    _CABLE_LIKE,
    _METRO_LIKE,
    _RAIL_LIKE,
    _TRAM_LIKE,
    _WATER_LIKE,
    DemandPoint,
    HEADWAY_STEPS,
    HeadwaySchedule,
    Journey,
    JourneyLeg,
    MODE_PARAMS,
    PassengerOptions,
    ROUTING_WINDOWS,
    W_RIDE,
    W_WAIT,
    W_WALK,
)

from .geo import (
    _haversine_m,
    _route_type_value,
    _stop_id,
    _stop_lat,
    _stop_lon,
    _stop_name,
)

from .time_model import (
    passenger_generalized_time,
    passenger_wait_s,
)


def passenger_mode_key(route_type: Any) -> str:
    """Ключ ``MODE_PARAMS`` по типу маршрута (``RouteType`` или строка)."""
    raw = route_type
    if not isinstance(raw, str):
        raw = getattr(raw, "value", raw)
    text = str(raw or "").strip().lower()
    if text in _BUS_LIKE:
        return "bus"
    if text in _TRAM_LIKE:
        return "tram"
    if text in _METRO_LIKE:
        return "metro"
    if text in _RAIL_LIKE:
        return "rail"
    if text in _WATER_LIKE:
        return "water"
    if text in _CABLE_LIKE:
        return "cable"
    return "bus"


class _Network:
    """Частотная сеть: остановки-узлы, поездки-рёбра, пешие переходы."""

    __slots__ = (
        "directions",
        "keys",
        "lats",
        "lons",
        "radii",
        "ride_from",
        "route_mode",
        "route_schedule",
        "stop_routes",
        "walk_from",
    )

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.lats: list[float] = []
        self.lons: list[float] = []
        # (route_id, dir_idx, seg_idx, to_idx, ride_s)
        self.ride_from: list[list[tuple[Any, int, int, int, float]]] = []
        # (to_idx, walk_s)
        self.walk_from: list[list[tuple[int, float]]] = []
        self.stop_routes: list[set[Any]] = []
        #: Расписание линии по окнам маршрутизации (§5.6a).
        self.route_schedule: dict[Any, HeadwaySchedule] = {}
        self.route_mode: dict[Any, str] = {}
        self.radii: list[float] = []
        self.directions: list[tuple[Any, int, tuple[int, ...]]] = []


def _node_key(stop: Any, lat: float | None, lon: float | None) -> str:
    sid = _stop_id(stop)
    if sid is not None:
        try:
            if isinstance(sid, bool):
                raise ValueError
            return f"id:{int(sid)}"  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return f"id:{sid}"
    if lat is not None and lon is not None:
        return f"geo:{lat:.6f},{lon:.6f}"
    name = _stop_name(stop)
    return f"name:{name}" if name else "anon"


def _build_network(
    routes: Any,
    headways: dict[Any, float] | None,
    options: PassengerOptions,
) -> _Network:
    """Строит частотную сеть из маршрутов (по ходу направлений)."""
    net = _Network()
    index: dict[str, int] = {}
    headways = dict(headways or {})

    def node(stop: Any) -> int | None:
        lat, lon = _stop_lat(stop), _stop_lon(stop)
        if lat is None or lon is None:
            return None
        key = _node_key(stop, lat, lon)
        existing = index.get(key)
        if existing is not None:
            return existing
        idx = len(net.keys)
        index[key] = idx
        net.keys.append(key)
        net.lats.append(lat)
        net.lons.append(lon)
        net.ride_from.append([])
        net.walk_from.append([])
        net.stop_routes.append(set())
        net.radii.append(0.0)
        return idx

    for route in routes or ():
        route_id = getattr(route, "route_id", None)
        if route_id is None:
            continue
        mode = passenger_mode_key(_route_type_value(route))
        params = MODE_PARAMS[mode]
        raw_headway = headways.get(route_id, params["default_headway_min"])
        schedule = HeadwaySchedule.from_value(raw_headway)
        if options.strict_headway:
            for code, _start, _end in ROUTING_WINDOWS:
                passenger_validate_headway(
                    schedule.minutes_for(code), strict=True
                )
        speed_mps = max(1.0, float(params["speed_kmh"]) * 1000.0 / 3600.0)
        dwell = float(params["dwell_s"])
        # Ходит ли линия хотя бы в одном окне. Остановки припаркованной линии
        # регистрируются как узлы (точка спроса существует независимо от
        # расписания), но **не получают** пешеходного радиуса: остановка,
        # куда никто не приезжает, не создаёт охвата (§12.6 — считаются
        # только остановки, где составы действительно останавливаются).
        runs_anywhere = any(
            schedule.is_running_in(code) for code, _s, _e in ROUTING_WINDOWS
        )
        for dir_idx, direction in enumerate(getattr(route, "directions", None) or ()):
            # Остановки регистрируются всегда, даже если линия стоит: точка
            # спроса существует независимо от того, ходит ли через неё что-то
            # (§12.6 — остановка не перестаёт быть остановкой).
            stops = list(getattr(direction, "stops", None) or ())
            order: list[int] = []
            for stop in stops:
                idx = node(stop)
                if idx is None:
                    continue
                order.append(idx)
                if not runs_anywhere:
                    continue
                net.stop_routes[idx].add(route_id)
                radius = float(params["walk_radius_m"])
                if radius > net.radii[idx]:
                    net.radii[idx] = radius
            if not schedule.is_running_in("W1") and not schedule.is_running_in(
                "W2"
            ) and not schedule.is_running_in("W3") and not schedule.is_running_in(
                "W0"
            ):
                continue  # линия не ходит ни в одном окне
            if len(order) < 2:
                continue
            net.route_schedule[route_id] = schedule
            net.route_mode[route_id] = mode
            net.directions.append((route_id, dir_idx, tuple(order)))
            for seg_idx in range(len(order) - 1):
                origin, dest = order[seg_idx], order[seg_idx + 1]
                dist = _haversine_m(
                    net.lats[origin], net.lons[origin],
                    net.lats[dest], net.lons[dest],
                )
                ride = dist * float(options.tortuosity) / speed_mps + dwell
                net.ride_from[origin].append((route_id, dir_idx, seg_idx, dest, ride))

    # Пешие переходы между остановками в радиусе пересадки (§6.3, 10 мин).
    limit_m = float(options.max_transfer_walk_s) * float(options.walk_speed_mps)
    cell = 0.01  # ~1 км
    grid: dict[tuple[int, int], list[int]] = {}
    for idx, (lat, lon) in enumerate(zip(net.lats, net.lons)):
        grid.setdefault((math.floor(lat / cell), math.floor(lon / cell)), []).append(idx)
    for idx, (lat, lon) in enumerate(zip(net.lats, net.lons)):
        cx, cy = math.floor(lat / cell), math.floor(lon / cell)
        seen: set[int] = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in grid.get((cx + dx, cy + dy), ()):
                    if other <= idx or other in seen:
                        continue
                    seen.add(other)
                    dist = _haversine_m(lat, lon, net.lats[other], net.lons[other])
                    if dist <= limit_m and dist > 0.0:
                        walk_s = dist / float(options.walk_speed_mps)
                        net.walk_from[idx].append((other, walk_s))
                        net.walk_from[other].append((idx, walk_s))
    return net


def _initial_states(
    net: _Network, origin_idx: int, options: PassengerOptions
) -> list[tuple[float, int]]:
    """Остановки в пешем подходе от остановки-начала (включая её саму).

    Используется как запасной вариант, когда точка спроса совпадает с узлом.
    """
    limit_m = float(options.max_walk_to_stop_s) * float(options.walk_speed_mps)
    olat, olon = net.lats[origin_idx], net.lons[origin_idx]
    found: list[tuple[float, int]] = []
    for idx, (lat, lon) in enumerate(zip(net.lats, net.lons)):
        dist = _haversine_m(olat, olon, lat, lon)
        if dist <= limit_m:
            found.append((dist / float(options.walk_speed_mps), idx))
    if not found:
        found.append((0.0, origin_idx))
    return found


class _StopIndex:
    """Сетка остановок для быстрого поиска пешеходного охвата (§6.3).

    Построена один раз на сеть: поиск остановок рядом с точкой спроса
    иначе стоил ``точки × остановки`` гаверсинусов, что на городе
    реального масштаба давало миллионы вызовов.
    """

    __slots__ = ("cell_deg", "cells", "lats", "lons", "radii")

    def __init__(self, net: "_Network") -> None:
        self.lats = net.lats
        self.lons = net.lons
        self.radii = net.radii
        # Ячейка ~1 км: пешеходный радиус любого режима кратен этому.
        self.cell_deg = 1.0 / _KM_PER_DEG_LAT
        self.cells: dict[tuple[int, int], list[int]] = {}
        for idx, (lat, lon) in enumerate(zip(net.lats, net.lons)):
            if net.radii[idx] <= 0.0:
                continue
            cell = (math.floor(lat / self.cell_deg), math.floor(lon / self.cell_deg))
            self.cells.setdefault(cell, []).append(idx)

    def nearby(self, lat: float, lon: float, radius_m: float) -> list[int]:
        """Индексы остановок в пределах ``radius_m`` от точки."""
        span = int(math.ceil(radius_m / 1000.0 / 1.0)) + 1
        clat = math.floor(lat / self.cell_deg)
        clon = math.floor(lon / self.cell_deg)
        found: list[int] = []
        for dx in range(-span, span + 1):
            for dy in range(-span, span + 1):
                bucket = self.cells.get((clat + dx, clon + dy))
                if bucket:
                    found.extend(bucket)
        return found


def _access_states(
    net: _Network,
    index: _StopIndex,
    point: DemandPoint,
    options: PassengerOptions,
) -> list[tuple[float, int]]:
    """Остановки, достижимые пешком от точки спроса, с временем подхода.

    Радиус берётся у остановки (пешеходный радиус режима, §7.1), но не
    больше общего предела подхода (§6.3, 45 мин). Точка спроса не обязана
    совпадать с остановкой — это отдельные сущности (§5.2).
    """
    hard_limit = float(options.max_walk_to_stop_s) * float(options.walk_speed_mps)
    walk_speed = max(0.1, float(options.walk_speed_mps))
    found: list[tuple[float, int]] = []
    seen: set[int] = set()
    for idx in index.nearby(point.lat, point.lon, hard_limit):
        if idx in seen:
            continue
        seen.add(idx)
        radius = net.radii[idx]
        if radius <= 0.0:
            continue
        limit = min(float(radius), hard_limit)
        dist = _haversine_m(point.lat, point.lon, net.lats[idx], net.lons[idx])
        if dist <= limit:
            found.append((dist / walk_speed, idx))
    if not found:
        # Точка не в пешем охвате ни одной остановки — пути на транспорте нет.
        found.append((math.inf, -1))
    return found


def _run_dijkstra(
    net: _Network,
    initial: list[tuple[float, int]],
    origin_key: str,
    options: PassengerOptions,
    window: str = "W1",
) -> tuple[
    dict[int, float],
    dict[tuple[int, Any], tuple[tuple[int, Any] | None, JourneyLeg | None]],
]:
    """Прогон от одного начала сразу до всех остановок (принцип §6.2).

    Состояние — ``(остановка, маршрут_в_салоне | None)``. Возвращает:

    - ``best`` — лучшее обобщённое время до остановки (уже вне салона);
    - ``parent`` — ``состояние → (предыдущее_состояние | None, участок)``.
      ``None`` в ``previous`` означает стартовое состояние, поэтому путь с
      прямым пешим доступом восстанавливается так же, как путь с посадкой.
    """
    max_boardings = max(1, int(options.max_transfers) + 1)
    best: dict[int, float] = {}
    parent: dict[tuple[int, Any], tuple[tuple[int, Any] | None, JourneyLeg | None]] = {}
    # куча: (обобщённое_время, порядковый_номер, остановка, маршрут, посадки).
    # Порядковый номер обязателен: при равных временах Python сравнил бы
    # ``None`` с числом в поле маршрута и упал бы с TypeError.
    heap: list[tuple[float, int, int, Any, int]] = []
    visited: dict[tuple[int, Any], float] = {}
    #: Лучшая **известная** стоимость состояния, включая ещё не извлечённые
    #: из кучи. Без неё проверка улучшения идёт по ``visited``, а ``visited``
    #: заполняется только при извлечении, — и тогда более дорогая релаксация
    #: успевает перезаписать ``parent`` состояния, лучшая версия которого ещё
    #: лежит в куче. Восстановленный путь тогда ведёт не туда, куда его
    #: посчитал Дейкстра, и ``gen_time`` перестаёт сходиться с суммой ног.
    settled: dict[tuple[int, Any], float] = {}
    order = 0

    for walk_s, stop in initial:
        if stop < 0 or not math.isfinite(walk_s):
            continue  # точка вне пешего охвата: стартового состояния нет
        gen = walk_s * W_WALK
        state_from = (stop, None)
        if gen < settled.get(state_from, math.inf):
            settled[state_from] = gen
            parent[state_from] = (
                None,
                JourneyLeg(
                    kind="access",
                    from_key=origin_key,
                    to_key=net.keys[stop],
                    time_s=walk_s,
                ),
            )
        heapq.heappush(heap, (gen, order, stop, None, 0))
        order += 1

    while heap:
        gen, _order, stop, onboard, boardings = heapq.heappop(heap)
        state = (stop, onboard)
        if visited.get(state, math.inf) <= gen:
            continue
        visited[state] = gen

        if onboard is None:
            if gen < best.get(stop, math.inf):
                best[stop] = gen
            # Посадка на каждый маршрут, обслуживающий остановку.
            if boardings < max_boardings:
                for route_id in net.stop_routes[stop]:
                    schedule = net.route_schedule.get(route_id)
                    if schedule is None:
                        continue
                    headway = schedule.minutes_for(window)
                    if headway is None:
                        continue  # линия в это окно не ходит
                    wait_add = passenger_wait_s(headway, options.arrival_gap_s)
                    state_to = (stop, route_id)
                    cost = gen + wait_add * W_WAIT
                    if settled.get(state_to, math.inf) <= cost:
                        continue
                    settled[state_to] = cost
                    parent[state_to] = (
                        state,
                        JourneyLeg(
                            kind="board",
                            from_key=net.keys[stop],
                            to_key=net.keys[stop],
                            time_s=wait_add,
                            route_id=route_id,
                        ),
                    )
                    heapq.heappush(
                        heap, (cost, order, stop, route_id, boardings + 1)
                    )
                    order += 1
            # Пеший переход: пересадка измеряется временем ходьбы (§6.3).
            for other, walk_add in net.walk_from[stop]:
                state_to = (other, None)
                cost = gen + walk_add * W_WALK
                if settled.get(state_to, math.inf) <= cost:
                    continue
                settled[state_to] = cost
                parent[state_to] = (
                    state,
                    JourneyLeg(
                        kind="walk",
                        from_key=net.keys[stop],
                        to_key=net.keys[other],
                        time_s=walk_add,
                    ),
                )
                heapq.heappush(heap, (cost, order, other, None, boardings))
                order += 1
        else:
            # Поездка по сегментам своего маршрута.
            for route_id, dir_idx, seg_idx, dest, ride_add in net.ride_from[stop]:
                if route_id != onboard:
                    continue
                state_to = (dest, onboard)
                cost = gen + ride_add * W_RIDE
                if settled.get(state_to, math.inf) <= cost:
                    continue
                settled[state_to] = cost
                parent[state_to] = (
                    state,
                    JourneyLeg(
                        kind="ride",
                        from_key=net.keys[stop],
                        to_key=net.keys[dest],
                        time_s=ride_add,
                        route_id=route_id,
                        segment=(route_id, dir_idx, seg_idx),
                    ),
                )
                heapq.heappush(
                    heap, (cost, order, dest, onboard, boardings)
                )
                order += 1
            # Выход из салона не стоит времени.
            state_to = (stop, None)
            # Сверка с ``settled``, а не с ``visited``: ``visited`` хранит
            # только принятые состояния, и условие ``visited > gen`` проходило
            # почти всегда, из-за чего выход перезаписывал ``parent`` дороже
            # того пути, который уже был найден. Дальше по этой цепочке
            # восстанавливался маршрут в никуда — с суммой часов пешего хода.
            if gen < settled.get(state_to, math.inf):
                settled[state_to] = gen
                parent[state_to] = (state, None)
                heapq.heappush(heap, (gen, order, stop, None, boardings))
                order += 1
    return best, parent


def _journey_from_parent(
    net: _Network,
    parent: dict[tuple[int, Any], tuple[tuple[int, Any] | None, JourneyLeg | None]],
    dest_idx: int,
    egress_s: float,
) -> Journey | None:
    """Восстанавливает поездку до остановки назначения плюс выход к цели.

    ``egress_s`` — пеший путь от остановки назначения до точки спроса;
    он добавляется к ``walk_s``, поэтому учитывается и во времени, и в
    диагностике. Возвращает ``None``, если состояние не посещалось.
    """
    state: tuple[int, Any] = (dest_idx, None)
    if state not in parent:
        return None
    legs: list[JourneyLeg] = []
    current: tuple[int, Any] | None = state
    guard = 0
    while current is not None and guard < 10_000:
        previous, leg = parent[current]
        if leg is not None:
            legs.append(leg)
        current = previous
        guard += 1
    legs.reverse()
    if egress_s > 0.0:
        legs.append(
            JourneyLeg(
                kind="egress",
                from_key=net.keys[dest_idx],
                to_key="",
                time_s=egress_s,
            )
        )
    ride = sum(leg.time_s for leg in legs if leg.kind == "ride")
    walk = sum(
        leg.time_s for leg in legs if leg.kind in ("access", "walk", "egress")
    )
    wait = sum(leg.time_s for leg in legs if leg.kind == "board")
    boardings: list[Any] = []
    segments: list[tuple[Any, int, int]] = []
    for leg in legs:
        if leg.kind != "ride":
            continue
        if not boardings or boardings[-1] != leg.route_id:
            boardings.append(leg.route_id)
        if leg.segment is not None:
            segments.append(leg.segment)
    return Journey(
        legs=tuple(legs),
        ride_s=ride,
        walk_s=walk,
        wait_s=wait,
        transfers=max(0, len(boardings) - 1),
        gen_time_s=passenger_generalized_time(ride, walk, wait),
        boardings=tuple(boardings),
        segments=tuple(segments),
    )


# ── Главный расчёт ────────────────────────────────────────────────────
