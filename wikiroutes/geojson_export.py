"""Экспорт остановок маршрутной сети в GeoJSON.

Все остановки маршрутов экспортируются как Point-фичи. Конечные остановки
(первая/последняя остановка направления) помечаются свойством
``is_terminal: true``.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from shapely.geometry import mapping as geojson_mapping

from .compat import stop_id, stop_lat, stop_lon, stop_name
from .support import fix_stop_coord

logger = logging.getLogger("wikiroutes.geojson_export")

__all__ = ["build_boundary_geojson", "build_terminals_geojson"]


def _stop_key(name: str, sid: Any) -> str:
    """Уникальный ключ остановки: id, если он есть, иначе нормализованное имя.

    Остановки с разными id считаются разными, даже если названия совпадают
    (одноимённые остановки на разных концах маршрута). Без id группируем по
    нормализованному названию. В отличие от ``stops.collect_unique_stops``,
    OSM-пары stop/platform (одно имя, соседние координаты) здесь остаются
    раздельными.
    """
    if sid is not None:
        return f"id:{sid}"
    norm = re.sub(r"[^a-zа-яё0-9]+", " ", str(name).strip().lower()).strip()
    return f"name:{norm}"


def _ingest_geojson_stop(
    stops: dict[str, dict[str, Any]],
    terminal_keys: set[str],
    route_name: str,
    dir_len: int,
    idx: int,
    stop: Any,
) -> None:
    """Добавляет остановку в индекс и помечает конечные терминалы."""
    sid = stop_id(stop)
    name = stop_name(stop)
    key = _stop_key(name, sid)
    record = stops.get(key)
    if record is None:
        record = stops[key] = {
            "name": name,
            "lat": stop_lat(stop),
            "lon": stop_lon(stop),
            "idx": idx,
            "routes": set(),
        }
    elif record["lat"] is None:
        lat = stop_lat(stop)
        lon = stop_lon(stop)
        if lat is not None and lon is not None:
            record["lat"] = lat
            record["lon"] = lon
            record["idx"] = idx
    record["routes"].add(route_name)

    if idx == 0 or idx == dir_len - 1:
        terminal_keys.add(key)


def _geojson_features(
    stops: dict[str, dict[str, Any]],
    terminal_keys: set[str],
    bbox: tuple[float, float, float, float] | None,
) -> list[dict[str, Any]]:
    """Feature-коллекция Point-фич с коррекцией координат."""
    features: list[dict[str, Any]] = []
    for key in sorted(stops, key=lambda k: stops[k]["name"]):
        rec = stops[key]
        lat, lon = rec["lat"], rec["lon"]
        if lat is None or lon is None:
            continue
        coord = fix_stop_coord(
            type("_S", (), {"latitude": lat, "longitude": lon})(),
            rec["idx"],
            bbox,
        )
        if coord is None:
            continue
        lat_f, lon_f = coord
        route_names = sorted(rec["routes"])
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon_f, lat_f]},
                "properties": {
                    "name": rec["name"],
                    "is_terminal": key in terminal_keys,
                    "routes": route_names,
                    "route_count": len(route_names),
                },
            }
        )
    return features


def _write_geojson(
    path: str | Path,
    city_title: str,
    features: Sequence[dict[str, Any]],
) -> Path | None:
    """Сохраняет FeatureCollection (устойчиво к ошибкам записи)."""
    path = Path(path)
    feature_collection = {
        "type": "FeatureCollection",
        "name": f"{city_title} — остановки",
        "features": features,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(feature_collection, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        logger.exception("GeoJSON не сохранён: %s", path)
        return None
    return path


def build_terminals_geojson(
    routes: Sequence[Any],
    city_title: str,
    path: str | Path,
    bbox: tuple[float, float, float, float] | None = None,
) -> Path | None:
    """Создаёт GeoJSON FeatureCollection всех остановок и сохраняет его.

    Конечные остановки (первая/последняя остановка направления) имеют
    ``is_terminal: true`` в ``properties``. Координаты в WGS-84 ``[lon, lat]``.
    Возвращает ``None`` при пустом результате или ошибке записи.
    """
    if not routes:
        return None

    stops: dict[str, dict[str, Any]] = {}
    terminal_keys: set[str] = set()

    for route in routes:
        if getattr(route, "error", None) or not getattr(route, "directions", None):
            continue
        route_name = str(getattr(route, "name", "") or "")

        for direction in route.directions:
            dir_stops = getattr(direction, "stops", None)
            if not dir_stops:
                continue
            dir_len = len(dir_stops)

            for idx, stop in enumerate(dir_stops):
                _ingest_geojson_stop(
                    stops, terminal_keys, route_name, dir_len, idx, stop
                )

    if not stops:
        logger.warning("GeoJSON: остановки не найдены")
        return None

    features = _geojson_features(stops, terminal_keys, bbox)
    if not features:
        logger.warning("GeoJSON: нет остановок с корректными координатами")
        return None

    return _write_geojson(path, city_title, features)


def build_boundary_geojson(
    boundary_geom: Any,
    source: str | None,
    city_title: str,
    path: str | Path,
) -> Path | None:
    """Сохранет полигон границы города в GeoJSON FeatureCollection.

    Геометрия ожидается в координатах ``(lon, lat)`` (WGS-84). Источник
    границы (``source``: ``"Overture-buildings"``/``"OSM"``/None)
    кладётся в ``properties.source``. Возвращает ``None``, если границы нет
    или запись не удалась.
    """
    if boundary_geom is None or boundary_geom.is_empty:
        logger.warning("GeoJSON границы: граница города не получена")
        return None
    path = Path(path)
    feature_collection = {
        "type": "FeatureCollection",
        "name": f"{city_title} — граница города",
        "features": [
            {
                "type": "Feature",
                "properties": {"source": source} if source else {},
                "geometry": geojson_mapping(boundary_geom),
            }
        ],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(feature_collection, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        logger.exception("GeoJSON границы не сохранён: %s", path)
        return None
    return path