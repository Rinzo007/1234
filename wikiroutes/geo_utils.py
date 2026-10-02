"""Общие GIS-утилиты: UTM, Shapely и объединение геометрий."""
import hashlib
import logging
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from shapely.errors import GEOSException

from errors import MissingDependencyError
from models import RouteData

logger = logging.getLogger("wikiroutes.gis.common")

def shapely_stack() -> dict[str, Any]:
    """Лениво загружает Shapely 2 API, используемый GIS-модулями."""
    try:
        import shapely
        from shapely import STRtree
        from shapely.geometry import LineString, mapping, shape
    except ImportError as exc:
        raise MissingDependencyError(
            "Для GIS-расчётов нужно установить: pip install shapely"
        ) from exc
    return {
        "shapely": shapely,
        "LineString": LineString,
        "mapping": mapping,
        "shape": shape,
        "STRtree": STRtree,
    }

def resolve_sources(path: str | None, extensions: Sequence[str]) -> list[str]:
    """Возвращает список файлов по пути, ограничивая его расширениями."""
    if not path:
        return []

    p = Path(path).expanduser().resolve()
    exts = tuple(ext.lower() for ext in extensions)

    if p.is_dir():
        return [
            str(f)
            for f in sorted(p.iterdir())
            if f.is_file() and f.name.lower().endswith(exts)
        ]

    if p.is_file():
        return [str(p)]

    return []

def route_geo_sig(route: RouteData) -> str:
    """Строит короткую стабильную сигнатуру геометрии маршрута."""
    points = [
        (round(lat, 4), round(lon, 4))
        for direction in route.directions
        for lat, lon in direction.coords
    ]
    return hashlib.blake2b(
        repr(points).encode("utf-8"),
        digest_size=5,
    ).hexdigest()[:10]

def utm_epsg(lons: Sequence[float], lats: Sequence[float]) -> int:
    """Возвращает EPSG-код подходящей UTM-зоны для набора координат."""
    if len(lons) == 0 or len(lats) == 0:
        raise ValueError("utm_epsg: нужны непустые списки lons/lats")

    mean_lon = sum(lons) / len(lons)
    mean_lat = sum(lats) / len(lats)

    if not (math.isfinite(mean_lon) and math.isfinite(mean_lat)):
        raise ValueError("utm_epsg: координаты содержат NaN/Inf")

    zone = int((mean_lon + 180.0) / 6.0) + 1
    zone = max(1, min(60, zone))
    hemisphere = 32700 if mean_lat < 0 else 32600
    return hemisphere + zone

# Ошибки Shapely/типов, для которых имеет смысл пробовать fallback-объединение.
_FALLBACK_ERRORS = (
    GEOSException,
    ValueError,
    TypeError,
    AttributeError,
    RuntimeError,
)


def _union_call(seq: Sequence[Any], shapely: Any, grid_size: float | None) -> Any:
    """Единый вызов ``shapely.union_all`` с учётом ``grid_size``."""
    if grid_size is not None:
        return shapely.union_all(seq, grid_size=grid_size)
    return shapely.union_all(seq)


def _merge_into(result: Any, geom: Any) -> Any | None:
    """Объединяет ``geom`` в результат; при неудаче возвращает ``None``."""
    try:
        return result.union(geom)
    except _FALLBACK_ERRORS:
        pass
    try:
        return result.union(geom.buffer(0))
    except _FALLBACK_ERRORS:
        return None


def _sequential_union_fallback(geoms: list[Any], shapely: Any) -> Any:
    """Последовательный union с попиксельным buffer(0) recovery."""
    result = geoms[0]
    for geom in geoms[1:]:
        merged = _merge_into(result, geom)
        if merged is not None:
            result = merged
    return result


def union_all(
    geoms: Sequence[Any], shapely: Any = None, grid_size: float | None = None
) -> Any | None:
    """Объединяет геометрии с fallback для топологических проблем.

    ``grid_size`` задаёт точность объединения (snap-to-grid): ускоряет оверлей
    и убирает микро-артефакты. Fallback-операции предназначены только для
    восстановления проблемных геометрий; проект требует Shapely 2 и не
    поддерживает старые API.
    """
    geoms = list(geoms)
    if not geoms:
        return None

    if shapely is None:
        import shapely

    try:
        return _union_call(geoms, shapely, grid_size)
    except _FALLBACK_ERRORS:
        logger.debug(
            "union_all: прямой вызов не удался, пробуем set_precision",
            exc_info=True,
        )

    try:
        return _union_call(
            [shapely.set_precision(g, 0.01) for g in geoms],
            shapely,
            grid_size,
        )
    except _FALLBACK_ERRORS:
        logger.debug(
            "union_all: set_precision не помог, пробуем buffer(0)",
            exc_info=True,
        )

    try:
        return _union_call([g.buffer(0) for g in geoms], shapely, grid_size)
    except _FALLBACK_ERRORS:
        logger.debug(
            "union_all: buffer(0) не помог, пробуем последовательный union",
            exc_info=True,
        )

    return _sequential_union_fallback(geoms, shapely)

