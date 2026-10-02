"""Сбор геометрии, UTM-проекция и профиль «fast» (dedup_analyze.prepare)."""

from __future__ import annotations

from typing import Any

import numpy as np
from pyproj.exceptions import CRSError, ProjError
from shapely.errors import GEOSException

from ...common import utm_epsg
from ...dedup.geometry import (
    DedupId,
    _project_routes,
    _type_label,
    logger,
)
from ...models import RouteData

_transformer_cache: dict[int, Any] = {}


def _get_transformer(transformer_cls: Any, epsg: int) -> Any:
    cached = _transformer_cache.get(epsg)
    if cached is None:
        cached = transformer_cls.from_crs(
            "EPSG:4326", f"EPSG:{epsg}", always_xy=True
        )
        _transformer_cache[epsg] = cached
    return cached


def _direction_coords(direction: Any) -> list[tuple[float, float]]:
    try:
        return [(lon, lat) for lat, lon in getattr(direction, "coords", [])]
    except (TypeError, ValueError, AttributeError):
        logger.debug("Cannot read direction coords", exc_info=True)
        return []


def _collect_units_geo(
    routes: list[RouteData],
) -> tuple[
    dict[DedupId, list[list[tuple[float, float]]]],
    dict[DedupId, dict[str, Any]],
]:
    """Геометрия направлений по единицам (per_direction=True)."""
    from ...units import build_units

    routes_geo: dict[DedupId, list[list[tuple[float, float]]]] = {}
    meta: dict[DedupId, dict[str, Any]] = {}
    for u in build_units(routes):
        key = u.key
        coords = _direction_coords(u)
        if len(coords) < 2:
            continue
        routes_geo[key] = [coords]
        meta[key] = {
            "type": _type_label(getattr(u, "route_type", "")),
            "name": getattr(u, "name", ""),
            "route_id": getattr(u, "route_id", None),
            "di": getattr(u, "di", None),
        }
    return routes_geo, meta


def _collect_route_variants(
    routes: list[RouteData],
) -> tuple[
    dict[DedupId, list[list[tuple[float, float]]]],
    dict[DedupId, dict[str, Any]],
]:
    """Геометрия и метаданные маршрутов по их route_id."""
    routes_geo: dict[DedupId, list[list[tuple[float, float]]]] = {}
    meta: dict[DedupId, dict[str, Any]] = {}
    for rd in routes:
        try:
            if getattr(rd, "error", False):
                continue
            directions = getattr(rd, "directions", None)
            if not directions:
                continue
            route_id = int(rd.route_id)
        except (TypeError, ValueError, AttributeError):
            logger.debug("Cannot parse route header", exc_info=True)
            continue
        variants: list[list[tuple[float, float]]] = []
        for d in directions:
            coords = _direction_coords(d)
            if len(coords) >= 2:
                variants.append(coords)
        if variants:
            routes_geo[route_id] = variants
            meta[route_id] = {
                "type": _type_label(getattr(rd, "route_type", "")),
                "name": getattr(rd, "name", ""),
            }
    return routes_geo, meta


def _collect_routes_geo(
    routes: list[RouteData], per_direction: bool
) -> tuple[
    dict[DedupId, list[list[tuple[float, float]]]],
    dict[DedupId, dict[str, Any]],
]:
    """Собирает геометрию маршрутов/направлений и их метаданные."""
    if per_direction:
        return _collect_units_geo(routes)
    return _collect_route_variants(routes)


def _collect_route_lons_lats(
    routes_geo: dict[DedupId, list[list[tuple[float, float]]]],
) -> tuple[list[float], list[float]]:
    lons: list[float] = []
    lats: list[float] = []
    for variants in routes_geo.values():
        for variant in variants:
            for lon, lat in variant:
                lons.append(lon)
                lats.append(lat)
    return lons, lats


def _fast_line(geom: Any, buffer_r: float, shapely_module: Any) -> Any:
    """Понижение точности геометрии для «fast»-профиля (Tier 2).

    Упрощение линий и привязка к сетке ускоряют буферизацию и оверлей, но
    слегка меняют покрытие — поэтому профиль opt-in и по умолчанию выключен.
    Если после понижения точности линия исчезает, возвращаем исходник, чтобы
    не потерять маршрут.
    """
    tol = max(buffer_r / 20.0, 1e-9)
    grid = max(buffer_r / 100.0, 1e-9)
    try:
        g = shapely_module.simplify(geom, tol)
        g = shapely_module.set_precision(g, grid)
    except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
        return geom
    if g is None or getattr(g, "is_empty", True):
        return geom
    return g


def _apply_fast_tier(
    lines: dict[DedupId, list[Any]],
    orig_ids: list[DedupId],
    buffer_r: float,
    shapely_module: Any,
) -> tuple[dict[DedupId, list[Any]], list[DedupId], list[DedupId]] | None:
    """Tier 2 профиля «fast»: векторное упрощение линий до grid-сетки.

    При ошибке оверлея откатывается на построчный ``_fast_line``. Возвращает
    ``None``, если после упрощения осталось меньше двух маршрутов — тогда
    анализ бессмысленен.
    """
    tol = max(buffer_r / 20.0, 1e-9)
    grid = max(buffer_r / 100.0, 1e-9)
    flat_fast: list[Any] = []
    flat_route_idx: list[int] = []
    for ri, route_id in enumerate(orig_ids):
        for line in lines[route_id]:
            flat_fast.append(line)
            flat_route_idx.append(ri)
    if not flat_fast:
        return lines, list(lines.keys()), list(orig_ids)
    try:
        flat_arr = np.asarray(flat_fast, dtype=object)
        simp = shapely_module.simplify(flat_arr, tol)
        prec = shapely_module.set_precision(simp, grid)
        bad = shapely_module.is_empty(prec)
        none_mask = np.asarray(
            [g is None for g in prec], dtype=bool
        )
        bad = bad | none_mask
        prec = np.where(bad, flat_arr, prec)
        per_route: dict[int, list[Any]] = {}
        for idx, geom in enumerate(prec):
            if geom is not None and not getattr(geom, "is_empty", True):
                per_route.setdefault(
                    flat_route_idx[idx], []).append(geom)
        lines = {
            rid: per_route.get(ri, [])
            for ri, rid in enumerate(orig_ids)
        }
    except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
        lines = {
            rid: [_fast_line(ln, buffer_r, shapely_module)
                  for ln in rls]
            for rid, rls in lines.items()
        }
    lines = {rid: rls for rid, rls in lines.items() if rls}
    if len(lines) < 2:
        return None
    # После упрощения могли исчезнуть маршруты — обновляем списки
    # идентификаторов, чтобы дальше не тащить пустые (KeyError при
    # обращении lines[route_id]).
    ids = list(lines.keys())
    return lines, ids, list(ids)


def _project_dedupe_eps(buffer_r: float, profile: str) -> float:
    """Сетка отсева дублей проекционных точек профиля."""
    if profile == "fast":
        return max(0.1, min(2.0, buffer_r / 50.0))
    return 0.01


def _prepare_data(
    routes: list[RouteData],
    per_direction: bool,
    buffer_r: float,
    profile: str,
    stack: dict[str, Any],
) -> tuple[dict[DedupId, list[list[tuple[float, float]]]], dict[DedupId, dict[str, Any]], dict[DedupId, list[Any]], list[DedupId], int] | None:
    """Сбор геометрии, UTM-проекция и профиль «fast».

    Возвращает ``None`` при недостаточном числе маршрутов, ошибках проекции
    или сбое профиля — тогда анализ бессмысленен.
    """
    shapely_module = stack["shapely"]
    routes_geo, meta = _collect_routes_geo(routes, per_direction)

    if len(routes_geo) < 2:
        return None

    lons, lats = _collect_route_lons_lats(routes_geo)
    if not lons or not lats:
        return None

    epsg = utm_epsg(lons, lats)
    if epsg is None:
        logger.warning("Dedup: не удалось определить UTM-зону, пропуск")
        return None
    try:
        transformer = _get_transformer(stack["Transformer"], int(epsg))
    except (CRSError, ProjError, TypeError, ValueError):
        logger.warning("Dedup: не удалось создать Transformer", exc_info=True)
        return None

    # Размер сетки отсева дублей точек проекции: для «fast» — крупнее
    # (дешевле), для точного профиля — мелкая сетка (поведение по умолчанию).
    lines = _project_routes(
        routes_geo,
        transformer,
        np,
        stack["LineString"],
        dedupe_eps=_project_dedupe_eps(buffer_r, profile),
    )
    lines = {route_id: rls for route_id, rls in lines.items() if rls}

    ids: list[DedupId] = list(lines.keys())
    if len(ids) < 2:
        return None

    # Tier 2 (профиль «fast»): понижение точности геометрии ускоряет
    # буферизацию и оверлей, но слегка меняет покрытие — opt-in (см. #8.1).
    if profile == "fast":
        fast_out = _apply_fast_tier(lines, ids, buffer_r, shapely_module)
        if fast_out is None:
            return None
        lines, ids, _ = fast_out

    return routes_geo, meta, lines, ids, int(epsg)