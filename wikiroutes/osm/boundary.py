"""Получение административной границы города из OSM через Nominatim.

Граница используется для отсева направлений маршрутов, выходящих за
пределы города. Результат (геометрия границы) кэшируется по имени города,
чтобы не обращаться к Nominatim при каждом запуске: Nominatim ограничивает
частоту запросов, и повторные ходы в сеть не нужны.

Координаты в ответе Nominatim — GeoJSON, то есть ``[lon, lat]``; shapely
при сборке геометрии трактует их как ``(x=lon, y=lat)``. Направления
маршрутов хранят координаты как ``(lat, lon)``, поэтому при проверке
попадания линия строится в порядке ``(lon, lat)`` (см. filters.py).
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import requests
from shapely import make_valid
from shapely.errors import ShapelyError
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from ..cache import JsonCache
from ..constants import NOMINATIM_URL
from .geom import (
    _assemble_relation_geom,
    _assemble_way_geom,
    _region_extra_relations,
    _region_level_relation,
    _region_level_way,
)

_OVERPASS_SERVERS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass.nchc.org.tw/api/interpreter",
]
from ..support import safe_float as _safe_float

logger = logging.getLogger("wikiroutes.osm.boundary")

_BOUNDARY_ERR_LOG = "osm_boundary_errors.log"
_BOUNDARY_CACHE_KIND = "osm_boundary"
_METERS_PER_DEG_LAT = 111_320.0
_MIN_BOUNDARY_AREA_DEG2 = 1e-5
_MAX_BOUNDARY_AREA_DEG2 = 5.0
_NOMINATIM_MIN_INTERVAL_S = 1.0
_LAST_NOMINATIM_CALL = 0.0
_RETRY_DELAY_S = 3.0


def _ensure_boundary_err_log() -> None:
    if getattr(logger, "_boundary_err_file_attached", False):
        return
    try:
        handler = logging.FileHandler(_BOUNDARY_ERR_LOG, encoding="utf-8")
    except OSError:
        return
    handler.setLevel(logging.WARNING)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    logger._boundary_err_file_attached = True


def _load_cached(
    cache: JsonCache, cache_key: str, max_area: float = _MAX_BOUNDARY_AREA_DEG2
) -> BaseGeometry | None:
    cached = cache.get(_BOUNDARY_CACHE_KIND, cache_key)
    if not isinstance(cached, dict):
        return None
    geojson = cached.get("geojson") or cached
    if not isinstance(geojson, dict) or not geojson.get("type"):
        return None
    try:
        geom = make_valid(shape(geojson))
    except (TypeError, ValueError, ShapelyError):
        logger.debug("Не удалось восстановить границу из кэша", exc_info=True)
        return None
    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        return None
    if geom.is_empty or geom.area < _MIN_BOUNDARY_AREA_DEG2:
        logger.debug("Кэш границы вырожден (площадь ~%.2e), игнорируем", geom.area)
        return None
    if geom.area > max_area:
        logger.debug("Кэш границы слишком велик (площадь ~%.2e), игнорируем", geom.area)
        return None
    return geom


def _throttle_nominatim() -> None:
    global _LAST_NOMINATIM_CALL
    now = time.monotonic()
    wait = _NOMINATIM_MIN_INTERVAL_S - (now - _LAST_NOMINATIM_CALL)
    if wait > 0:
        time.sleep(wait)
    _LAST_NOMINATIM_CALL = time.monotonic()


def _store_cached(cache: JsonCache, cache_key: str, geom: BaseGeometry) -> None:
    try:
        cache.put(_BOUNDARY_CACHE_KIND, cache_key, geom.__geo_interface__)
    except (OSError, TypeError, ValueError):
        logger.debug("Не удалось сохранить границу в кэш", exc_info=True)


def _boundary_rank(item: dict[str, Any]) -> int:
    cls = item.get("category") or item.get("class")
    typ = item.get("type")
    at = item.get("addresstype")
    is_city = (
        (cls == "boundary" and typ in ("city", "municipality", "town", "village"))
        or at in ("city", "municipality", "town", "village")
    )
    if is_city:
        return 0
    if cls == "boundary":
        return 1
    return 2


def _pick_boundary_polygon(
    results: list[dict[str, Any]],
    *,
    max_area: float = _MAX_BOUNDARY_AREA_DEG2,
) -> BaseGeometry | None:
    candidates: list[tuple[int, float, float, float, BaseGeometry]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        geojson = item.get("geojson")
        if not isinstance(geojson, dict):
            continue
        if geojson.get("type") not in ("Polygon", "MultiPolygon"):
            logger.debug(
                "Кандидат границы отклонён: geojson=%s name=%r",
                geojson.get("type"),
                item.get("display_name"),
            )
            continue
        try:
            geom = make_valid(shape(geojson))
        except (TypeError, ValueError, ShapelyError):
            logger.debug("Некорректный полигон границы в ответе OSM", exc_info=True)
            continue
        if geom.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        if geom.is_empty or geom.area < _MIN_BOUNDARY_AREA_DEG2:
            logger.debug(
                "Кандидат границы отклонён (вырожден, площадь ~%.2e): name=%r",
                geom.area,
                item.get("display_name"),
            )
            continue
        if geom.area > max_area:
            logger.debug(
                "Кандидат границы отклонён (слишком велик, площадь ~%.2e): name=%r",
                geom.area,
                item.get("display_name"),
            )
            continue
        candidates.append(
            (
                _boundary_rank(item),
                -_safe_float(item.get("importance")),
                -float(geom.area),
                -_safe_float(item.get("place_rank", 0)),
                geom,
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda entry: entry[:4])
    return candidates[0][4]


def _fetch_via_nominatim(
    session: requests.Session,
    name: str,
    cc: str | None,
    *,
    max_area: float,
) -> BaseGeometry | None:
    attempts: list[tuple[dict[str, str], str | None]] = []
    if cc:
        attempts.append(({"city": name}, cc))
        attempts.append(({"q": name}, cc))
    attempts.append(({"city": name}, None))
    attempts.append(({"q": name}, None))
    last_exc: Exception | None = None
    for attempt, used_cc in attempts:
        params = {
            **attempt,
            "format": "jsonv2",
            "polygon_geojson": 1,
            "limit": 10,
            "addressdetails": 0,
            "extratags": 1,
        }
        if used_cc:
            params["countrycodes"] = used_cc
        _throttle_nominatim()
        try:
            resp = session.get(NOMINATIM_URL, params=params, timeout=(10.0, 60.0))
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            _ensure_boundary_err_log()
            logger.warning(
                "Не удалось получить границу «%s» из OSM (запрос %s): %s",
                name,
                attempt,
                exc,
            )
            continue
        if not isinstance(data, list) or not data:
            logger.debug("OSM не вернул результатов для «%s» (запрос %s)", name, attempt)
            continue
        geom = _pick_boundary_polygon(data, max_area=max_area)
        if geom is not None:
            return geom
        logger.debug("Запрос %s не дал пригодного полигона для «%s»", attempt, name)
    if last_exc is not None:
        _ensure_boundary_err_log()
        logger.warning("Не удалось получить границу «%s» из OSM: %s", name, last_exc)
    else:
        _ensure_boundary_err_log()
        logger.warning("В ответе OSM нет полигона границы для «%s»", name)
    return None


def _overpass_post(
    session: requests.Session,
    server_url: str,
    query: str,
) -> dict | None:
    server_name = server_url.split("/")[2]
    try:
        _throttle_nominatim()
        resp = session.post(
            server_url,
            data={"data": query},
            timeout=(20.0, 120.0),
            headers={"User-Agent": "WikiroutesExporter/1.0"},
        )
        if resp.status_code in (429, 504):
            logger.warning(
                "Overpass %s вернул %d на запрос %s…",
                server_name,
                resp.status_code,
                query[:60],
            )
            return None
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning(
            "Overpass %s недоступен (запрос %s…): %s",
            server_name,
            query[:60],
            exc,
        )
        return None


def _fetch_via_overpass(
    session: requests.Session,
    element_id: int,
    *,
    max_area: float = _MAX_BOUNDARY_AREA_DEG2,
    element_type: str = "relation",
) -> BaseGeometry | None:
    if element_type not in ("relation", "way"):
        raise ValueError(f"Неподдерживаемый тип элемента OSM: {element_type!r}")
    query = f"[out:json][timeout:90];{element_type}({element_id});out geom;"
    assemble = (
        _assemble_relation_geom
        if element_type == "relation"
        else _assemble_way_geom
    )
    for server_url in random.sample(_OVERPASS_SERVERS, len(_OVERPASS_SERVERS)):
        data = _overpass_post(session, server_url, query)
        if data is None:
            time.sleep(_RETRY_DELAY_S)
            data = _overpass_post(session, server_url, query)
        if data is None:
            continue
        for element in data.get("elements", []):
            if element.get("type") != element_type:
                continue
            geom = assemble(element)
            if geom is None or geom.geom_type not in ("Polygon", "MultiPolygon"):
                continue
            if geom.is_empty or geom.area < _MIN_BOUNDARY_AREA_DEG2:
                logger.debug(
                    "Граница %s %s вырождена (площадь ~%.2e)",
                    element_type,
                    element_id,
                    geom.area,
                )
                continue
            if geom.area > max_area:
                logger.debug(
                    "Граница %s %s слишком велика (площадь ~%.2e)",
                    element_type,
                    element_id,
                    geom.area,
                )
                continue
            return geom
        logger.debug(
            "Overpass %s не вернул пригодной геометрии для %s %s",
            server_url.split("/")[2],
            element_type,
            element_id,
        )
    logger.warning(
        "Не удалось получить %s %s: все зеркала Overpass недоступны",
        element_type,
        element_id,
    )
    return None


def _fetch_extra_relations(
    session: requests.Session,
    cache: JsonCache,
    relation_ids: tuple[int, ...],
    *,
    refresh: bool = False,
) -> list[BaseGeometry]:
    """Загружает дополнительные relation (муниципалитет и т.п.) с кэшем."""
    geoms: list[BaseGeometry] = []
    for rid in relation_ids:
        rel_cache_key = f"relation:{rid}"
        cached = _load_cached(cache, rel_cache_key)
        if cached is not None and not refresh:
            geoms.append(cached)
            continue
        geom = _fetch_via_overpass(session, rid)
        if geom is not None:
            _store_cached(cache, rel_cache_key, geom)
            geoms.append(geom)
        else:
            logger.warning(
                "Не удалось получить дополнительную границу relation %s "
                "(Overpass недоступен); пропущена.",
                rid,
            )
    return geoms


def _merge_boundaries(geometries: list[BaseGeometry]) -> BaseGeometry | None:
    """Объединяет несколько границ в одну (unary_union), возвращает None при пустом входе."""
    valid = [g for g in geometries if g is not None and not g.is_empty]
    if not valid:
        return None
    if len(valid) == 1:
        return valid[0]
    merged = unary_union(valid)
    if merged.geom_type not in ("Polygon", "MultiPolygon"):
        logger.warning("Склейка границ дала неожиданную геометрию: %s", merged.geom_type)
        return None
    if merged.is_empty:
        return None
    return merged


def fetch_city_boundary(
    city: str,
    session: requests.Session,
    cache: JsonCache,
    *,
    refresh: bool = False,
    countrycodes: str | None = None,
    extra_relation_ids: tuple[int, ...] = (),
) -> BaseGeometry | None:
    cache_key = city.strip().lower()
    if not cache_key:
        return None

    main: BaseGeometry | None = None

    def _fetch_element(
        element_type: str, element_id: int, label: str
    ) -> BaseGeometry | None:
        elem_cache_key = f"{element_type}:{element_id}"
        cached = _load_cached(cache, elem_cache_key)
        if cached is not None and not refresh:
            return cached
        geom = _fetch_via_overpass(
            session, element_id, element_type=element_type
        )
        if geom is not None:
            _store_cached(cache, elem_cache_key, geom)
        else:
            logger.warning(
                "Не удалось получить границу по %s %s (Overpass недоступен); "
                "Nominatim для %s-городов не используется.",
                element_type,
                element_id,
                label,
            )
        return geom

    element_type, element_id = None, None
    relation_id = _region_level_relation(cache_key)
    if relation_id is not None:
        element_type, element_id = "relation", relation_id
    else:
        way_id = _region_level_way(cache_key)
        if way_id is not None:
            element_type, element_id = "way", way_id
    if element_type is not None:
        main = _fetch_element(element_type, element_id, element_type)
    else:
        cached = _load_cached(cache, cache_key)
        if cached is not None and not refresh:
            main = cached
        else:
            geom = _fetch_via_nominatim(
                session, city, countrycodes, max_area=_MAX_BOUNDARY_AREA_DEG2,
            )
            if geom is not None:
                _store_cached(cache, cache_key, geom)
                main = geom

    extra = _fetch_extra_relations(
        session,
        cache,
        _combine_extra_ids(cache_key, extra_relation_ids),
        refresh=refresh,
    )
    parts = [g for g in ([main] + extra) if g is not None]
    return _merge_boundaries(parts)


def _combine_extra_ids(
    cache_key: str, explicit: tuple[int, ...]
) -> tuple[int, ...]:
    """Собирает relation-id муниципалитетов: реестровые по умолчанию + явные."""
    registry = _region_extra_relations(cache_key)
    combined = list(registry) + list(explicit)
    return tuple(dict.fromkeys(combined))


def meters_to_deg_lat(meters: float) -> float:
    return max(0.0, float(meters)) / _METERS_PER_DEG_LAT


__all__ = ["fetch_city_boundary", "meters_to_deg_lat"]
