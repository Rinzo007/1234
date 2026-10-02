"""Дедупликация: геометрические примитивы и вспомогательные вычисления."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from functools import cache
from typing import Any, TypeAlias

import numpy as np
import pandas as pd
from pyproj.exceptions import CRSError, ProjError
from shapely.errors import GEOSException

from ..errors import MissingDependencyError
from ..support import safe_float as _safe_float
from ..support import type_label
from ..type_defs import DirectionKey

logger = logging.getLogger("wikiroutes.gis.dedup")

DedupId: TypeAlias = int | DirectionKey

def _finite_positive_max(series: pd.Series) -> float:
    """Возвращает положительный максимум серии; при отсутствии/NaN возвращает 1.0."""
    if len(series) == 0:
        return 1.0
    value = _safe_float(series.max(), default=1.0)
    if value <= 0.0:
        return 1.0
    return value

def _type_label(value: Any) -> str:
    """Безопасно получает человекочитаемый тип маршрута."""
    try:
        return type_label(value)
    except (TypeError, AttributeError):
        return ""

@cache
def _dedup_stack() -> dict[str, Any]:
    """Импорт современных зависимостей для дедупликации.

    Результат кэшируется: модули — синглтоны, а вызовов (cache key, analyze,
    network_after) много, поэтому повторный импорт не нужен.
    """
    try:
        import shapely
        from pyproj import Transformer
        from shapely import STRtree
        from shapely.geometry import LineString
    except ImportError as exc:
        raise MissingDependencyError(
            "Для --dedup нужно установить: pip install numpy pandas pyproj shapely"
        ) from exc
    return {
        "shapely": shapely,
        "Transformer": Transformer,
        "LineString": LineString,
        "STRtree": STRtree,
    }

def _skip_empty(geom: Any) -> bool:
    if geom is None:
        return True
    try:
        return bool(geom.is_empty)
    except (AttributeError, TypeError):
        return True


def _geom_subparts(geom: Any) -> tuple[Any, ...]:
    try:
        return tuple(geom.geoms)
    except (AttributeError, TypeError):
        return ()


def _line_parts(geom: Any) -> Iterator[Any]:
    """Возвращает только линейные части геометрии."""
    if _skip_empty(geom):
        return

    geom_type = getattr(geom, "geom_type", "")
    if geom_type == "LineString":
        yield geom
        return
    if geom_type == "MultiLineString":
        yield from _geom_subparts(geom)
        return
    if geom_type == "GeometryCollection":
        for part in _geom_subparts(geom):
            yield from _line_parts(part)

def _bounds_overlap(
    bounds_a: tuple[float, float, float, float],
    bounds_b: tuple[float, float, float, float],
) -> bool:
    """Пересекаются ли прямоугольники (bbox) двух геометрий."""
    minx_a, miny_a, maxx_a, maxy_a = bounds_a
    minx_b, miny_b, maxx_b, maxy_b = bounds_b
    return not (
        maxx_a < minx_b or maxx_b < minx_a or maxy_a < miny_b or maxy_b < miny_a
    )

_VECTOR_LENGTH_MIN = 4

def _safe_lengths(shapely_module: Any, geoms: Any) -> Any | None:
    """Векторно считает длины геометрий; при ошибке возвращает None."""
    try:
        arr = shapely_module.length(np.atleast_1d(geoms))
        return np.asarray(arr, dtype=np.float64).reshape(-1)
    except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
        return None

def _sum_lengths_loop(geoms: Any) -> float:
    """Поточечное суммирование длин (fallback и быстрый путь для малых наборов)."""
    total = 0.0
    for geom in np.atleast_1d(geoms):
        try:
            length = float(getattr(geom, "length", float("nan")))
        except (TypeError, ValueError, AttributeError):
            continue
        if np.isfinite(length):
            total += length
    return total

def _sum_part_lengths(intersection: Any) -> float:
    """Суммирует длины линейных частей пересечения."""
    total = 0.0
    for part in _line_parts(intersection):
        try:
            length = float(part.length)
        except (TypeError, ValueError, AttributeError):
            continue
        if np.isfinite(length):
            total += length
    return total


def _covered_length_loop(lines: Any, other_buffer: Any) -> float:
    """Поточечная реализация (fallback при ошибке батч-пересечения)."""
    total = 0.0
    for line in lines:
        if line is None or getattr(line, "is_empty", True):
            continue
        try:
            total += _sum_part_lengths(line.intersection(other_buffer))
        except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
            continue
    return total

def _covered_length_candidates(
    lines: Any,
    line_bounds: Any | None,
    buffer_bounds: tuple[float, float, float, float],
) -> list[Any]:
    """Линии-кандидаты, чей bbox пересекается с bbox буфера."""
    if line_bounds is not None and len(line_bounds) == len(lines):
        return [
            line
            for line, bounds in zip(lines, line_bounds)
            if _bounds_overlap(bounds, buffer_bounds)
        ]
    return [
        line
        for line in lines
        if line is not None
        and not getattr(line, "is_empty", True)
        and _bounds_overlap(line.bounds, buffer_bounds)
    ]


def _single_line_covered_length(line: Any, other_buffer: Any) -> float:
    """Быстрый путь для одной линии: intersection без промежуточных массивов."""
    try:
        length = float(line.intersection(other_buffer).length)
    except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
        return 0.0
    return length if np.isfinite(length) else 0.0


def _batch_covered_length(
    keep: list[Any], other_buffer: Any, shapely_module: Any
) -> float:
    """Батч-путь: один vectorized intersection и суммирование длин."""
    try:
        intersection = shapely_module.intersection(
            np.asarray(keep, dtype=object),
            other_buffer,
        )
    except (GEOSException, TypeError, ValueError, RuntimeError):
        return _covered_length_loop(keep, other_buffer)

    geoms = np.atleast_1d(intersection)
    if len(geoms) >= _VECTOR_LENGTH_MIN:
        lengths = _safe_lengths(shapely_module, geoms)
        if lengths is not None:
            finite = lengths[np.isfinite(lengths)]
            return float(finite.sum())
    return _sum_lengths_loop(geoms)


def _covered_length(
    lines: Any,
    other_buffer: Any,
    shapely_module: Any,
    line_bounds: Any | None = None,
) -> float:
    """Суммарная длина частей линий, попавших в буфер другого маршрута.

    ``line_bounds`` — предвычисленные кортежи bounds линий маршрута; если не
    задан, bounds вычисляются на месте. Горячий путь сознательно бесNumPy'ный:
    в per-direction режиме набор почти всегда состоит из одной линии.
    """
    try:
        if lines is None or other_buffer is None:
            return 0.0
        if getattr(other_buffer, "is_empty", True):
            return 0.0

        buffer_bounds = other_buffer.bounds
        keep = _covered_length_candidates(lines, line_bounds, buffer_bounds)

        if not keep:
            return 0.0
        if len(keep) == 1:
            return _single_line_covered_length(keep[0], other_buffer)
        return _batch_covered_length(keep, other_buffer, shapely_module)
    except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError) as exc:
        logger.warning("Intersection failed in _covered_length for %s", type(exc).__name__)
        return 0.0

def _dedupe_xy_loop(
    points: list[tuple[float, float]],
    eps: float,
) -> list[tuple[float, float]]:
    """Поточечная реализация (fallback для нерегулярных данных)."""
    out: list[tuple[float, float]] = []
    last_grid: tuple[float, float] | None = None
    for x, y in points:
        if not (np.isfinite(x) and np.isfinite(y)):
            continue
        try:
            gx = round(x / eps) * eps
            gy = round(y / eps) * eps
        except (TypeError, ValueError, OverflowError):
            continue
        grid_key = (gx, gy)
        if last_grid != grid_key:
            out.append((x, y))
            last_grid = grid_key
    return out


def _dedupe_xy_loop_ndarray(points: Any, eps: float) -> np.ndarray:
    """Fallback-вариант :func:`_dedupe_xy`, возвращающий (K, 2) ndarray."""
    out = _dedupe_xy_loop(points, eps)
    if not out:
        return np.zeros((0, 2), dtype=np.float64)
    return np.asarray(out, dtype=np.float64)


def _dedupe_xy(
    points: Any,
    eps: float = 0.01,
) -> np.ndarray:
    """Удаляет последовательные дубли точек после округления до сетки.

    Принимает (M, 2) ndarray (как координаты проекции) и возвращает (K, 2)
    ndarray — без лишних преобразований tuple→ndarray→tuple, которые раньше
    делались между proj-трансформом и конструктором LineString.
    """
    if eps <= 0.0:
        return np.asarray(points, dtype=np.float64)
    try:
        arr = np.asarray(points, dtype=np.float64)
    except (TypeError, ValueError, OverflowError):
        arr = None
    if arr is None or arr.ndim != 2 or arr.shape[1] < 2 or arr.shape[0] == 0:
        return _dedupe_xy_loop_ndarray(points, eps)
    finite = np.isfinite(arr).all(axis=1)
    if not finite.any():
        return arr[:0].reshape(0, 2)
    arr = arr[finite]
    grid = np.round(arr / eps) * eps
    keep = np.empty(arr.shape[0], dtype=bool)
    keep[0] = True
    keep[1:] = (grid[1:] != grid[:-1]).any(axis=1)
    return arr[keep]

def _project_variant(
    variant: list[tuple[float, float]],
    transformer: Any,
    np_module: Any,
    LineString: Any,
    dedupe_eps: float,
) -> Any | None:
    """Проецирует один вариант маршрута в UTM и строит LineString (или None)."""
    if len(variant) < 2:
        return None
    try:
        arr = np_module.asarray(variant, dtype=np.float64)
    except (TypeError, ValueError):
        logger.debug("Cannot convert variant to numpy array")
        return None
    if arr.ndim != 2 or arr.shape[0] < 2 or arr.shape[1] < 2:
        return None
    try:
        xs, ys = transformer.transform(arr[:, 0], arr[:, 1])
    except (CRSError, ProjError, TypeError, ValueError, RuntimeError):
        logger.debug("Transformer failed")
        return None
    xs = np_module.asarray(xs, dtype=np.float64)
    ys = np_module.asarray(ys, dtype=np.float64)
    mask = np_module.isfinite(xs) & np_module.isfinite(ys)
    if not mask.any():
        return None
    xs = xs[mask]
    ys = ys[mask]
    pts = _dedupe_xy(np.column_stack([xs, ys]), dedupe_eps)
    if len(pts) < 2:
        pts = np.column_stack([xs, ys])
    if len(pts) < 2:
        return None
    try:
        return LineString(pts)
    except (TypeError, ValueError):
        logger.debug("Cannot create LineString")
        return None


def _project_routes(
    routes_geo: dict[DedupId, list[list[tuple[float, float]]]],
    transformer: Any,
    np_module: Any,
    LineString: Any,
    dedupe_eps: float = 0.01,
) -> dict[DedupId, list[Any]]:
    """Проецирует координаты маршрутов в UTM и строит LineString.

    ``dedupe_eps`` — размер сетки (в метрах проекции) для отсева
    последовательных дублирующихся точек. Для «fast»-профиля передаётся
    более крупная сетка (см. :func:`dedup_analyze`), что дешевле и достаточно
    для грубого отсева вырожденных петель.
    """
    lines: dict[DedupId, list[Any]] = {}
    for route_id, variants in routes_geo.items():
        route_lines: list[Any] = []
        for variant in variants:
            line = _project_variant(
                variant, transformer, np_module, LineString, dedupe_eps
            )
            if line is not None:
                route_lines.append(line)
        lines[route_id] = route_lines
    return lines

