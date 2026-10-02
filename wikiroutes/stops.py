"""Утилиты работы с остановками.

Модуль объединяет корректировку координат и два формата агрегации уникальных
остановок.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from .compat import stop_id, stop_lat, stop_lon, stop_name
from .models import RouteData, UniqueStop
from .support import fix_stop_coord, in_bbox

__all__ = [
    "collect_unique_stops",
    "collect_unique_stops_raw",
    "fix_stop_coord",
    "in_bbox",
    "merge_near_same_name_stops",
]

# OSM описывает один физический пункт парой нод с разными id: роль ``stop``
# (highway=bus_stop на дороге) и роль ``platform`` (посадочная площадка).
# Ключ по ``stop.id`` считал бы такую пару дважды; остановки одного имени в
# пределах этого радиуса схлопываются в одну уникальную.
_SAME_NAME_MERGE_M = 150.0


def _normalize_stop_name(name: str | None) -> str:
    """Нормализует название остановки для построения стабильного ключа."""
    text = str(name or "").strip().lower()
    text = " ".join(text.split())
    return text.strip(" ,;:!?-.")


def _stops_merge_m(first: tuple[float, float], second: tuple[float, float]) -> bool:
    """Попадают ли две точки (lat, lon) в радиус ``_SAME_NAME_MERGE_M``."""
    lat1, lon1 = first
    lat2, lon2 = second
    cos_mid = math.cos(math.radians((lat1 + lat2) / 2.0))
    dx = (lon2 - lon1) * cos_mid * 111_320.0
    dy = (lat2 - lat1) * 111_320.0
    return math.hypot(dx, dy) <= _SAME_NAME_MERGE_M


def _rounded_coord(lat: float | None, lon: float | None) -> tuple[float, float] | None:
    """Округлённая до 6 знаков координата для стабильного ключа кластера."""
    if lat is None or lon is None:
        return None
    return (round(float(lat), 6), round(float(lon), 6))


def _dominant_id(counts: dict[Any, int]) -> int | None:
    """id с наибольшим числом вхождений; при равенстве — первый встреченный."""
    best = None
    best_count = -1
    for value, count in counts.items():
        if count > best_count:
            best, best_count = value, count
    return best


class _StopCluster:
    """Схлопывание остановок одного имени, лежащих рядом.

    Группирует члены одного нормализованного имени, попавшие в радиус
    ``_SAME_NAME_MERGE_M``, в один кластер. Одноимённые остановки на разных
    концах маршрута (дальше порога) остаются отдельными записями.
    """

    def __init__(self) -> None:
        self._clusters: dict[str, dict[str, Any]] = {}
        self._name_chains: dict[str, list[str]] = {}

    @staticmethod
    def _new_record(name: str) -> dict[str, Any]:
        return {
            "name": name,
            "ids": {},
            "coords": {},
            "rep": None,
            "types": set(),
            "routes": set(),
            "idx": None,
        }

    def _cluster_key(
        self,
        norm: str,
        sid: int | None,
        latlon: tuple[float, float] | None,
    ) -> str:
        """Ключ кластера: имя с номером цепи, иначе как раньше — по id."""
        if not norm or latlon is None:
            return f"id:{sid}" if sid is not None else "anon"
        chain = self._name_chains.get(norm)
        if chain is None:
            self._name_chains[norm] = [norm]
            return norm
        for cluster_key in chain:
            rep = self._clusters[cluster_key]["rep"]
            if rep is not None and _stops_merge_m(rep, latlon):
                return cluster_key
        key = f"{norm}#{len(chain) + 1}"
        chain.append(key)
        return key

    def add(
        self,
        name: str | None,
        sid: int | None,
        lat: float | None,
        lon: float | None,
        stop_index: int | None,
        route_type: Any,
        route_id: Any,
    ) -> None:
        """Добавляет остановку в кластер и обновляет агрегаты."""
        norm = _normalize_stop_name(name)
        latlon = _rounded_coord(lat, lon)
        key = self._cluster_key(norm, sid, latlon)
        record = self._clusters.get(key)
        if record is None:
            record = self._clusters[key] = self._new_record(name)
        if record["idx"] is None:
            record["idx"] = stop_index
        if sid is not None:
            record["ids"][sid] = record["ids"].get(sid, 0) + 1
        if latlon is not None:
            record["coords"][latlon] = record["coords"].get(latlon, 0) + 1
            if (
                record["rep"] is None
                or record["coords"][latlon] > record["coords"][record["rep"]]
            ):
                record["rep"] = latlon
        record["types"].add(route_type)
        record["routes"].add(route_id)

    def items(self) -> Iterable[tuple[str, dict[str, Any]]]:
        return self._clusters.items()


def collect_unique_stops(
    routes: Iterable[RouteData],
) -> dict[str, UniqueStop]:
    """Собирает уникальные остановки в структурированные ``UniqueStop``."""
    cluster = _StopCluster()
    for route in routes:
        if not route.ok:
            continue
        for direction in route.directions:
            for stop in direction.stops:
                cluster.add(
                    stop.name,
                    stop.id,
                    stop.latitude,
                    stop.longitude,
                    None,
                    route.route_type,
                    route.route_id,
                )

    unique: dict[str, UniqueStop] = {}
    for key, record in cluster.items():
        lat, lon = record["rep"] if record["rep"] is not None else (None, None)
        unique[key] = UniqueStop(
            key=key,
            id=_dominant_id(record["ids"]),
            name=record["name"],
            latitude=lat,
            longitude=lon,
            route_types=frozenset(record["types"]),
            route_ids=frozenset(record["routes"]),
        )
    return unique


def collect_unique_stops_raw(
    routes: Iterable[RouteData],
) -> dict[str, dict[str, Any]]:
    """Собирает уникальные остановки в raw-формате для экспортного слоя."""
    cluster = _StopCluster()
    for route in routes:
        if route.error or not route.directions:
            continue
        for direction in route.directions:
            for stop_index, stop in enumerate(direction.stops):
                cluster.add(
                    stop_name(stop),
                    stop_id(stop),
                    stop_lat(stop),
                    stop_lon(stop),
                    stop_index,
                    route.route_type,
                    route.route_id,
                )

    unique: dict[str, dict[str, Any]] = {}
    for key, record in cluster.items():
        lat, lon = record["rep"] if record["rep"] is not None else (None, None)
        unique[key] = {
            "id": _dominant_id(record["ids"]),
            "name": record["name"],
            "lat": lat,
            "lon": lon,
            "idx": record["idx"] if record["idx"] is not None else 0,
            "types": record["types"],
            "routes": record["routes"],
        }
    return unique


def merge_near_same_name_stops(stops: Iterable[Any]) -> list[Any]:
    """Схлопывает одноимённые остановки в пределах ``_SAME_NAME_MERGE_M``.

    Связка нод ``stop``/``platform`` одного физического пункта имеет разные
    OSM-id, поэтому без схлопывания каждая пара считалась бы дважды. Возвращает
    остановки в исходном порядке первого вхождения, представляя кластер первой
    из вошедших в него остановок. Одноимённые пункты на разных концах маршрута
    (дальше порога) остаются отдельными записями.
    """
    cluster = _StopCluster()
    for stop in stops:
        cluster.add(
            stop_name(stop),
            stop_id(stop),
            stop_lat(stop),
            stop_lon(stop),
            None,
            None,
            None,
        )

    merged: list[Any] = []
    seen: set[str] = set()
    for stop in stops:
        key = cluster._cluster_key(
            _normalize_stop_name(stop_name(stop)),
            stop_id(stop),
            _rounded_coord(stop_lat(stop), stop_lon(stop)),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(stop)
    return merged