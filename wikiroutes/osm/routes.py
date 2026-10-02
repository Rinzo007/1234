"""Извлечение маршрутов общественного транспорта из OSM (Overpass).

Превращает отношения ``route``/``route_master`` в доменные модели:

* каждое отношение ``route`` становится направлением ``Direction``
  (геометрия склеивается из сегментов way-членов, остановки — из node-членов
  с ролями ``stop``/``platform``);
* направления группируются в ``RouteData`` по типу транспорта и номеру
  (``ref``), идентификатор маршрута берётся из ``route_master`` или
  минимального id входящего отношения.

Запрос к Overpass выполняется по bbox (обычно — граница города). Ответ
кэшируется в ``JsonCache`` (kind ``osm_routes``), поэтому повторные запуски
не обращаются в сеть.
"""

from __future__ import annotations

import hashlib
import logging
import random
from typing import Any

from shapely.geometry.base import BaseGeometry

from ..cache import JsonCache
from ..enums import RouteType
from ..http_client import SessionProvider
from ..models import Direction, RouteData, Stop
from ..report import Reporter
from ..stops import merge_near_same_name_stops
from ..transport_class import mentions_electrobus
from .boundary import (
    _OVERPASS_SERVERS,
    _overpass_post,
    fetch_city_boundary,
)

logger = logging.getLogger("wikiroutes.osm.routes")

# OSM route=* → доменный RouteType. share_taxi объединяется с bus (как minibus
# в parse_route_type), light_rail — с train.
_OSM_ROUTE_TYPES: dict[str, RouteType] = {
    "bus": RouteType.BUS,
    "share_taxi": RouteType.BUS,
    "trolleybus": RouteType.TROLLEYBUS,
    "tram": RouteType.TRAM,
    "metro": RouteType.METRO,
    "train": RouteType.TRAIN,
    "light_rail": RouteType.TRAIN,
    "funicular": RouteType.FUNICULAR,
    "cable": RouteType.CABLE,
    "monorail": RouteType.MONORAIL,
    "ferry": RouteType.WATER,
}

OSM_ROUTE_PATTERN = "|".join(_OSM_ROUTE_TYPES)

# Роли way-членов, которые не являются полотном маршрута (остановки, платформы).
_SKIP_WAY_ROLES = {
    "stop",
    "platform",
    "platform_entry_only",
    "platform_exit_only",
    "from",
    "to",
    "via",
}

# Роли node-членов, считающиеся остановками.
_STOP_ROLES = {"stop", "platform", "platform_entry_only", "platform_exit_only"}

_CACHE_KIND = "osm_routes"
_CACHE_VERSION = 1
_OSM_RELATION_URL = "https://www.openstreetmap.org/relation/{relation_id}"

# Сырой ответ Overpass для города весит в десятки МБ (геометрия всех way +
# теги всех node маршрутов), поэтому для этого kind нужен лимит больше
# общесетевого 8 МБ.
OSM_CACHE_ENTRY_BYTES = 512 * 1024 * 1024


def osm_route_type(value: object) -> RouteType | None:
    """Маппит OSM ``route=*`` на доменный ``RouteType``; неизвестные → None."""
    return _OSM_ROUTE_TYPES.get(str(value or "").strip().lower())


def _pt_query(bbox: tuple[float, float, float, float]) -> str:
    """Overpass-запрос всех PT-отношений в bbox с рекурсией вниз.

    route и route_master выбираются одним запросом (Overpass дедуплицирует
    членов, поэтому дублирования геометрии не возникает); ``(._;>;)``
    добавляет все way/node-члены, ``out body geom`` возвращает геометрию
    членов и теги узлов для остановок.
    """
    min_lat, min_lon, max_lat, max_lon = bbox
    return (
        f"[out:json][timeout:180];("
        f'relation["type"="route"]["route"~"^({OSM_ROUTE_PATTERN})$"]'
        f"({min_lat},{min_lon},{max_lat},{max_lon});"
        f'relation["type"="route_master"]["route_master"~"^({OSM_ROUTE_PATTERN})$"]'
        f"({min_lat},{min_lon},{max_lat},{max_lon});"
        f");(._;>;);out body geom;"
    )


def _fetch_payload(session: Any, query: str) -> dict[str, Any] | None:
    """Выполняет Overpass-запрос с фейловером по зеркалам.

    Порядок зеркал перемешивается на каждый запрос, чтобы нагрузка
    распределялась, а первое зеркало в списке (часто самое загруженное)
    не получало все тяжёлые PT-запросы.
    """
    for server_url in random.sample(_OVERPASS_SERVERS, len(_OVERPASS_SERVERS)):
        data = _overpass_post(session, server_url, query)
        if data is None:
            continue
        if data.get("elements"):
            return data
    logger.warning(
        "Overpass не вернул данных для запроса %s… (все зеркала недоступны)",
        query[:60],
    )
    return None


def _is_route_master(element: dict[str, Any]) -> bool:
    return (
        element.get("type") == "relation"
        and element.get("tags", {}).get("type") == "route_master"
    )


def _fetch_pt_data(
    session: Any,
    bbox: tuple[float, float, float, float],
) -> dict[str, list[dict[str, Any]]] | None:
    """Загружает relations route и route_master в указанном bbox.

    Элементы узлов/way сохраняются в поле ``routes`` вместе с отношениями
    ``route`` (именно из них собираются остановки и геометрия).
    """
    data = _fetch_payload(session, _pt_query(bbox))
    if data is None:
        return None
    elements = data.get("elements", [])
    return {
        "routes": [element for element in elements if not _is_route_master(element)],
        "masters": [element for element in elements if _is_route_master(element)],
    }


def _cache_key(city_slug: str, bbox: tuple[float, float, float, float]) -> str:
    digest = hashlib.sha256(repr(bbox).encode("utf-8")).hexdigest()[:10]
    return f"{city_slug.strip().lower()}:pt:v{_CACHE_VERSION}:{digest}"


def _cached_pt_data(
    cache: JsonCache,
    city_slug: str,
    bbox: tuple[float, float, float, float],
    refresh: bool,
    session: Any,
) -> dict[str, list[dict[str, Any]]] | None:
    """Обращение к Overpass с кэшированием ответа по (город, bbox)."""
    cache_key = _cache_key(city_slug, bbox)
    if not refresh:
        cached = cache.get(_CACHE_KIND, cache_key)
        if isinstance(cached, dict) and isinstance(cached.get("routes"), list):
            return cached
    data = _fetch_pt_data(session, bbox)
    if data is None:
        return None
    try:
        cache.put(_CACHE_KIND, cache_key, data)
    except (OSError, TypeError, ValueError):
        logger.debug("Не удалось сохранить кэш OSM-маршрутов", exc_info=True)
    return data


# ---------------------------------------------------------------------------
# Разбор и сборка геометрии
# ---------------------------------------------------------------------------


def _pt_eq(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6


def _stitch_route_coords(
    segments: list[list[tuple[float, float]]],
) -> list[tuple[float, float]]:
    """Склеивает сегменты way-членов в упорядоченную линию (lat, lon).

    Жадный подбор по общим концам с поддержкой развёрнутых сегментов;
    остатки (разрыв геометрии одного направления) дописываются без перестановки,
    чтобы не потерять ни одну точку маршрута.
    """
    if not segments:
        return []
    unused = [list(seg) for seg in segments]
    chain = list(unused.pop(0))
    while unused:
        extended = False
        head, tail = chain[0], chain[-1]
        for i, seg in enumerate(unused):
            if _pt_eq(tail, seg[0]):
                chain.extend(seg[1:])
            elif _pt_eq(tail, seg[-1]):
                chain.extend(reversed(seg[:-1]))
            elif _pt_eq(head, seg[0]):
                chain = list(reversed(seg[1:])) + chain
            elif _pt_eq(head, seg[-1]):
                chain = list(seg[:-1]) + chain
            else:
                continue
            unused.pop(i)
            extended = True
            break
        if not extended:
            break
    for seg in unused:
        chain.extend(seg)
    return chain


def _member_coords(geometry: Any) -> list[tuple[float, float]] | None:
    """Список (lat, lon) из geometry члена отношения."""
    if not isinstance(geometry, list):
        return None
    coords: list[tuple[float, float]] = []
    for point in geometry:
        if not isinstance(point, dict) or "lat" not in point or "lon" not in point:
            continue
        coords.append((float(point["lat"]), float(point["lon"])))
    return coords if coords else None


def _route_segments(relation: dict[str, Any]) -> list[list[tuple[float, float]]]:
    """Сегменты полотна маршрута из way-членов (без остановок/платформ)."""
    segments: list[list[tuple[float, float]]] = []
    for member in relation.get("members", []):
        if member.get("type") != "way":
            continue
        if member.get("role") in _SKIP_WAY_ROLES:
            continue
        coords = _member_coords(member.get("geometry"))
        if coords and len(coords) >= 2:
            segments.append(coords)
    return segments


def _route_stops(
    relation: dict[str, Any],
    node_index: dict[int, dict[str, Any]],
) -> tuple[Stop, ...]:
    """Остановки направления из node-членов с ролями stop/platform.

    OSM описывает один физический пункт парой нод с разными id (``stop`` +
    ``platform``), поэтому одноимённые в пределах ``_SAME_NAME_MERGE_M``
    схлопываются в одну остановку, сохраняя порядок первого вхождения.
    """
    stops: list[Stop] = []
    for member in relation.get("members", []):
        if member.get("type") != "node":
            continue
        if member.get("role") not in _STOP_ROLES:
            continue
        node = node_index.get(int(member.get("ref")))
        if node is None or not isinstance(node, dict):
            continue
        stop = Stop.from_api(
            {
                "name": node.get("tags", {}).get("name", ""),
                "id": node.get("id"),
                "latitude": node.get("lat"),
                "longitude": node.get("lon"),
            }
        )
        if stop is not None:
            stops.append(stop)
    return tuple(merge_near_same_name_stops(stops))


def _direction_name(relation: dict[str, Any]) -> str:
    """Название направления: from → to, иначе name отношения."""
    tags = relation.get("tags", {})
    frm = str(tags.get("from") or "").strip()
    to = str(tags.get("to") or "").strip()
    if frm and to:
        return f"{frm} → {to}"
    return str(tags.get("name") or "").strip()


def _relation_direction(
    relation: dict[str, Any],
    node_index: dict[int, dict[str, Any]],
) -> Direction | None:
    """Строит ``Direction`` из отношения ``route``; None для пустой геометрии."""
    coords = _stitch_route_coords(_route_segments(relation))
    if len(coords) < 2:
        return None
    name = _direction_name(relation)
    if not name:
        name = str(relation.get("tags", {}).get("ref") or relation.get("id") or "")
    return Direction(
        coords=tuple(coords),
        stops=_route_stops(relation, node_index),
        name=name,
    )


def _node_index(elements: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {
        int(element["id"]): element
        for element in elements or ()
        if element.get("type") == "node" and element.get("id") is not None
    }


# ---------------------------------------------------------------------------
# Группировка в RouteData
# ---------------------------------------------------------------------------


def _relation_ref(relation: dict[str, Any]) -> str:
    tags = relation.get("tags", {})
    ref = str(tags.get("ref") or "").strip()
    if ref:
        return ref
    return str(tags.get("name") or "").strip() or str(relation.get("id", ""))


def _osm_electrobus(tags: dict[str, Any]) -> bool:
    """True, если теги отношения указывают на обслуживание электробусами.

    OSM не имеет отдельного ``route=electrobus``: электробусные маршруты
    помечаются как ``route=bus`` и упоминаются в теге ``description`` (или
    ``note``/``name``), как в «Маршрут обслуживается электробусами».
    """
    return any(
        mentions_electrobus(str(tags.get(key) or ""))
        for key in ("description", "note", "name")
    )


def _osm_route_type(relation: dict[str, Any]) -> RouteType | None:
    """Доменный тип маршрута OSM; автобус-электробус → ``ELECTROBUS``."""
    tags = relation.get("tags", {})
    route_type = osm_route_type(tags.get("route"))
    if route_type == RouteType.BUS and _osm_electrobus(tags):
        return RouteType.ELECTROBUS
    return route_type


def _group_key(route_type: RouteType, relation: dict[str, Any]) -> tuple[Any, ...]:
    tags = relation.get("tags", {})
    return (
        route_type.value,
        str(tags.get("network") or "").strip(),
        _relation_ref(relation),
    )


def _master_relation_id(
    master: dict[str, Any],
    route_type: RouteType,
    group_key: tuple[Any, ...],
) -> bool:
    """Подходит ли route_master для группы (тип транспорта и номер)."""
    if osm_route_type(master.get("tags", {}).get("route_master")) != route_type:
        return False
    network = str(master.get("tags", {}).get("network") or "").strip()
    if network != group_key[1]:
        return False
    ref = str(master.get("tags", {}).get("ref") or "").strip()
    if ref:
        return ref == group_key[2]
    return str(master.get("tags", {}).get("name") or "").strip() == group_key[2]


def build_routes_from_osm(
    route_elements: list[dict[str, Any]],
    master_elements: list[dict[str, Any]] | None = None,
    type_filter: frozenset[RouteType] | None = None,
    disabled_types: frozenset[RouteType] = frozenset(),
) -> list[RouteData]:
    """Собирает ``RouteData`` из элементов ответа Overpass (route + route_master).

    - отношение ``route`` → направление;
    - направления с одинаковым ``(тип, network, ref)`` → один ``RouteData``;
    - ``route_id`` берётся из ``route_master`` (по типу и номеру) или из
      минимального id отношения группы.
    """
    node_index = _node_index(route_elements)

    grouped: dict[tuple[Any, ...], list[Direction]] = {}
    relation_ids: dict[tuple[Any, ...], list[int]] = {}

    for relation in route_elements:
        if relation.get("type") != "relation":
            continue
        tags = relation.get("tags", {})
        if tags.get("type") != "route":
            continue
        route_type = _osm_route_type(relation)
        if route_type is None:
            continue
        if disabled_types and route_type in disabled_types:
            continue
        if type_filter is not None and type_filter and route_type not in type_filter:
            continue
        direction = _relation_direction(relation, node_index)
        if direction is None:
            continue
        key = _group_key(route_type, relation)
        grouped.setdefault(key, []).append(direction)
        relation_ids.setdefault(key, []).append(int(relation.get("id")))

    master_ids: dict[tuple[Any, ...], int] = {}
    for master in master_elements or ():
        if master.get("type") != "relation":
            continue
        master_type = osm_route_type(master.get("tags", {}).get("route_master"))
        if master_type is None:
            continue
        name = _relation_ref(master)
        key = (
            master_type.value,
            str(master.get("tags", {}).get("network") or "").strip(),
            name,
        )
        if _master_relation_id(master, master_type, key):
            master_ids[key] = int(master.get("id"))

    routes: list[RouteData] = []
    for key, directions in grouped.items():
        route_type = RouteType(key[0])
        route_id = master_ids.get(key) or min(relation_ids[key])
        routes.append(
            RouteData(
                name=key[2],
                route_type=route_type,
                route_id=route_id,
                url=_OSM_RELATION_URL.format(relation_id=route_id),
                directions=tuple(directions),
                source="osm",
            )
        )
    routes.sort(key=lambda route: (route.route_type.value, route.name))
    return routes


# ---------------------------------------------------------------------------
# Загрузка из сети/кэша
# ---------------------------------------------------------------------------


def _geom_bbox(geom: BaseGeometry | None) -> tuple[float, float, float, float] | None:
    """(min_lat, min_lon, max_lat, max_lon) с полем вокруг геометрии."""
    if geom is None or geom.is_empty or geom.area <= 0:
        return None
    minx, miny, maxx, maxy = geom.bounds
    pad = max(0.01, (maxy - miny + maxx - minx) * 0.02)
    return (miny - pad, minx - pad, maxy + pad, maxx + pad)


def osm_source_bbox(
    city_slug: str,
    *,
    cache: JsonCache,
    sessions: SessionProvider,
    config: Any,
    fallback_bbox: tuple[float, float, float, float] | None,
    boundary_geom: BaseGeometry | None = None,
) -> tuple[float, float, float, float] | None:
    """Bbox для Overpass-запроса: граница города (если включена) или fallback."""
    if config is not None and config.boundary and boundary_geom is not None:
        bbox = _geom_bbox(boundary_geom)
        if bbox is not None:
            return bbox
        logger.warning("Не удалось получить границу города для bbox OSM-маршрутов")
    if config is not None and config.boundary:
        geom = fetch_city_boundary(
            city_slug,
            sessions.get(),
            cache,
            countrycodes=config.boundary_country,
            extra_relation_ids=config.boundary_extra,
        )
        bbox = _geom_bbox(geom)
        if bbox is not None:
            return bbox
        logger.warning("Не удалось получить границу города для bbox OSM-маршрутов")
    return fallback_bbox


def load_osm_routes(
    city_slug: str,
    *,
    cache: JsonCache,
    sessions: SessionProvider,
    reporter: Reporter,
    bbox: tuple[float, float, float, float] | None = None,
    refresh: bool = False,
    config: Any = None,
    type_filter: frozenset[RouteType] | None = None,
    disabled_types: frozenset[RouteType] = frozenset(),
) -> list[RouteData]:
    """Загружает и собирает маршруты ОТ из OSM в указанном bbox.

    Bbox разрешается через ``osm_source_bbox`` (граница города), если не
    передан явно. Сетевой ответ кэшируется в ``JsonCache``; повторные запуски
    с тем же bbox не обращаются в Overpass.
    """
    if bbox is None:
        bbox = osm_source_bbox(
            city_slug,
            cache=cache,
            sessions=sessions,
            config=config,
            fallback_bbox=None,
        )
    if bbox is None:
        reporter.line("  ⚠ OSM-маршруты: не удалось определить bbox — пропущено")
        return []
    if not all(isinstance(value, (int, float)) for value in bbox):
        reporter.line("  ⚠ OSM-маршруты: некорректный bbox — пропущено")
        return []

    data = _cached_pt_data(cache, city_slug, bbox, refresh, sessions.get())
    if data is None:
        reporter.line("  ⚠ OSM-маршруты: Overpass недоступен — пропущено")
        return []
    routes = build_routes_from_osm(
        data.get("routes", []),
        data.get("masters", []),
        type_filter=type_filter,
        disabled_types=disabled_types,
    )
    reporter.line(f"  Добавлено маршрутов из OSM: {len(routes)}")
    return routes


__all__ = [
    "OSM_ROUTE_PATTERN",
    "build_routes_from_osm",
    "load_osm_routes",
    "osm_route_type",
    "osm_source_bbox",
]