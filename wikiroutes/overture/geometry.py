"""Ремонт геометрий и пространственные запросы к зданиям Overture."""

import hashlib
import logging
from typing import Any

import numpy as np
import shapely
from shapely.errors import GEOSException

from .context import _OvertureContext

logger = logging.getLogger("wikiroutes.gis.overture")


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ РАБОТЫ С МАССИВАМИ ГЕОМЕТРИЙ
# ============================================================================

def _geometry_wkb_fallback(geom: Any) -> bytes:
    try:
        return shapely.to_wkt(geom).encode()
    except (GEOSException, TypeError, ValueError, AttributeError):
        return repr(geom).encode()


def _geometry_degraded_hash(geom: Any) -> bytes:
    """Хеш одной геометрии, деградируя по мере доступности форматов."""
    try:
        g = shapely.normalize(geom)
        wkb = shapely.to_wkb(g)
    except (GEOSException, TypeError, ValueError, AttributeError):
        wkb = _geometry_wkb_fallback(geom)
    return hashlib.blake2b(wkb, digest_size=16).digest()


def _geometry_chunk_hashes(chunk: np.ndarray, out: list[bytes]) -> None:
    """Хеши массива геометрий, при ошибке векторизации — скалярно."""
    try:
        norm = shapely.normalize(chunk)
        wkbs = shapely.to_wkb(norm)
        out.extend(hashlib.blake2b(w, digest_size=16).digest() for w in wkbs)
    except (GEOSException, TypeError, ValueError):
        for geom in chunk:
            out.append(_geometry_degraded_hash(geom))


def _geometry_hashes(geoms: Any, chunk_size: int = 10_000) -> list[bytes]:
    """Возвращает список хешей геометрий, обрабатывая чанками."""
    arr = np.asarray(geoms, dtype=object)
    if arr.size == 0:
        return []
    out: list[bytes] = []
    for start in range(0, len(arr), chunk_size):
        _geometry_chunk_hashes(arr[start: start + chunk_size], out)
    return out


def _validate_bbox(bbox: Any) -> tuple[float, float, float, float]:
    min_lat, min_lon, max_lat, max_lon = map(float, bbox)
    if not (min_lat < max_lat and min_lon < max_lon):
        raise ValueError(
            f"min должен быть меньше max: "
            f"lat [{min_lat}, {max_lat}], lon [{min_lon}, {max_lon}]"
        )
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise ValueError(f"Широта вне диапазона [-90, 90]: {min_lat}, {max_lat}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise ValueError(f"Долгота вне диапазона [-180, 180]: {min_lon}, {max_lon}")
    return min_lat, min_lon, max_lat, max_lon


# ============================================================================
# РЕМОНТ ГЕОМЕТРИЙ (ВСПОМОГАТЕЛЬНЫЕ ШАГИ)
# ============================================================================

def _drop_empty_loop(arr: np.ndarray) -> tuple[np.ndarray, int]:
    """Скалярный fallback: удаляет None и пустые геометрии."""
    kept: list[Any] = []
    removed = 0
    for g in arr:
        try:
            if g is not None and not shapely.is_empty(g):
                kept.append(g)
            else:
                removed += 1
        except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
            removed += 1
    return np.asarray(kept, dtype=object), removed


def _drop_empty_and_none(arr: np.ndarray) -> tuple[np.ndarray, int]:
    """Удаляет None и пустые геометрии, возвращает (очищенный массив, количество удалённых)."""
    if arr.size == 0:
        return arr, 0
    removed = 0

    # Удаляем None
    none_mask = np.fromiter((g is None for g in arr), dtype=bool, count=arr.size)
    if none_mask.any():
        removed += int(none_mask.sum())
        arr = arr[~none_mask]
    if arr.size == 0:
        return arr, removed

    # Удаляем пустые геометрии
    try:
        empty_mask = shapely.is_empty(arr)
        if empty_mask.any():
            removed += int(empty_mask.sum())
            arr = arr[~empty_mask]
        return arr, removed
    except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
        # Fallback: скалярная проверка
        arr, fallback_removed = _drop_empty_loop(arr)
        return arr, removed + fallback_removed


def _force_2d(arr: np.ndarray) -> np.ndarray:
    """Приводит геометрии к 2D (игнорирует ошибки)."""
    try:
        return shapely.force_2d(arr)
    except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
        return arr


def _fix_invalid_loop(arr: np.ndarray) -> tuple[np.ndarray, int]:
    """Скалярный fallback: make_valid для невалидных, невалидные — удаляются."""
    fixed: list[Any] = []
    removed = 0
    for g in arr:
        try:
            if g is None or shapely.is_empty(g):
                removed += 1
                continue
            if not shapely.is_valid(g):
                g = shapely.make_valid(g)
            if g is None or shapely.is_empty(g):
                removed += 1
                continue
            fixed.append(g)
        except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
            removed += 1
    return np.asarray(fixed, dtype=object), removed


def _fix_invalid(arr: np.ndarray) -> tuple[np.ndarray, int]:
    """Исправляет невалидные геометрии через make_valid, возвращает (исправленный массив, количество удалённых).

    Если все геометрии — простые полигоны (тип 3) без Z, пропускает
    ``is_valid`` (Overture данные валидны по Construction; дорогая
    проверка стоит ~0.15s на 150k).
    """
    if arr.size == 0:
        return arr, 0
    try:
        type_ids = shapely.get_type_id(arr)
        has_z = shapely.has_z(arr)
        if np.all((type_ids == 3) & (~has_z)):
            return arr, 0
        invalid_mask = ~shapely.is_valid(arr)
        if invalid_mask.any():
            arr = arr.copy()
            arr[invalid_mask] = shapely.make_valid(arr[invalid_mask])
        return arr, 0
    except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
        return _fix_invalid_loop(arr)


def _polygonal_mask(parts: np.ndarray) -> np.ndarray:
    return np.isin(shapely.get_type_id(parts), (3, 6)) & (~shapely.is_empty(parts))


def _merge_polygonal_parts(parts: np.ndarray) -> Any | None:
    """Объединяет полигональные части коллекции в единый полигон (или None)."""
    for _ in range(4):
        polygonal_parts = parts[_polygonal_mask(parts)]
        if polygonal_parts.size == 0:
            return None
        merged = shapely.union_all(polygonal_parts)
        if merged is None or shapely.is_empty(merged):
            return None
        type_id = shapely.get_type_id(merged)
        if type_id in (3, 6):
            return merged
        if type_id != 7:
            return None
        parts = shapely.get_parts(merged)
    return None


def _extract_polygons_from_collections(arr: np.ndarray) -> tuple[np.ndarray, int]:
    """
    Извлекает полигональные части из GeometryCollection (тип 7).
    Возвращает (массив полигонов/мультиполигонов, количество удалённых).
    """
    if arr.size == 0:
        return arr, 0
    removed = 0
    type_ids = shapely.get_type_id(arr)
    gc_mask = type_ids == 7
    if not gc_mask.any():
        return arr, 0

    # Оставляем только полигоны и мультиполигоны (типы 3, 6)
    poly_mask = np.isin(type_ids, (3, 6))
    result = list(arr[poly_mask])

    # Обрабатываем каждую коллекцию
    for g in arr[gc_mask]:
        try:
            parts = shapely.get_parts(g)
            if parts.size == 0:
                removed += 1
                continue
            merged = _merge_polygonal_parts(parts)
            if merged is None:
                removed += 1
                continue
            result.append(merged)
        except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
            removed += 1

    if not result:
        return np.asarray([], dtype=object), removed
    return np.asarray(result, dtype=object), removed


def _filter_valid_loop(arr: np.ndarray) -> tuple[np.ndarray, int]:
    """Скалярный fallback: оставляет только валидные полигоны/мультиполигоны."""
    kept: list[Any] = []
    removed = 0
    for g in arr:
        try:
            if shapely.is_valid(g):
                kept.append(g)
            else:
                removed += 1
        except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
            removed += 1
    return np.asarray(kept, dtype=object), removed


def _filter_valid_polygons(arr: np.ndarray, *, skip_final_validation: bool = False) -> tuple[np.ndarray, int]:
    """Оставляет только полигоны/мультиполигоны (типы 3,6); остальные считаются невалидными.

    ``skip_final_validation=True`` пропускает ``is_valid`` (уже сделал
    ``make_valid`` в ``_fix_invalid`` — геометрии валидны гарантированно).
    """
    if arr.size == 0:
        return arr, 0
    removed = 0
    type_ids = shapely.get_type_id(arr)
    poly_mask = np.isin(type_ids, (3, 6))
    other_mask = ~poly_mask
    if other_mask.any():
        removed += int(other_mask.sum())
        arr = arr[poly_mask]
    if arr.size == 0:
        return arr, removed
    if skip_final_validation:
        return arr, removed
    # Проверка валидности (на случай, если make_valid не сработал)
    try:
        valid_mask = shapely.is_valid(arr)
        if not valid_mask.all():
            removed += int((~valid_mask).sum())
            arr = arr[valid_mask]
        return arr, removed
    except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
        kept_arr, loop_removed = _filter_valid_loop(arr)
        return kept_arr, removed + loop_removed


def _repair_polygonal_geometries(geoms: Any) -> tuple[np.ndarray, int]:
    """
    Основная функция ремонта полигональных геометрий.
    Возвращает (массив исправленных геометрий, количество удалённых/невалидных).
    """
    try:
        arr = np.asarray(geoms, dtype=object)
        if arr.size == 0:
            return arr, 0

        # Шаг 1: удалить None и пустые
        arr, removed = _drop_empty_and_none(arr)
        if arr.size == 0:
            return arr, removed

        # Шаг 2: привести к 2D
        arr = _force_2d(arr)

        # Шаг 3: исправить невалидные
        arr, invalid = _fix_invalid(arr)
        removed += invalid
        if arr.size == 0:
            return arr, removed

        # Шаг 4: извлечь полигоны из коллекций
        arr, invalid = _extract_polygons_from_collections(arr)
        removed += invalid
        if arr.size == 0:
            return arr, removed

        # Шаг 5: отфильтровать только полигоны/мультиполигоны
        # (is_valid не нужен — make_valid уже сделал их валидными)
        arr, invalid = _filter_valid_polygons(arr, skip_final_validation=True)
        removed += invalid

        return arr, removed

    except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
        logger.exception("Overture: векторный repair не удался, fallback")
        return _repair_polygonal_loop(geoms)


def _extract_polygon_from_collection(geom: Any) -> Any | None:
    """Объединяет полигональные части GeometryCollection в геометрию (или None)."""
    parts = shapely.get_parts(geom)
    if parts.size == 0:
        return None
    part_type_ids = shapely.get_type_id(parts)
    part_empty = shapely.is_empty(parts)
    mask = np.isin(part_type_ids, (3, 6)) & (~part_empty)
    polygonal_parts = parts[mask]
    if polygonal_parts.size == 0:
        return None
    merged = shapely.union_all(polygonal_parts)
    if (
        merged is None
        or shapely.is_empty(merged)
        or shapely.get_type_id(merged) not in (3, 6)
    ):
        return None
    return merged


def _repair_polygonal_single(geom: Any) -> Any | None:
    """Правит одну геометрию: полигон/мультиполигон/коллекция, иначе None."""
    if geom is None or shapely.is_empty(geom):
        return None
    type_id = shapely.get_type_id(geom)
    if type_id not in (3, 6, 7):
        return None
    if not shapely.is_valid(geom):
        geom = shapely.make_valid(geom)
    if geom is None or shapely.is_empty(geom):
        return None
    type_id = shapely.get_type_id(geom)
    if type_id in (3, 6):
        return geom
    if type_id == 7:
        return _extract_polygon_from_collection(geom)
    return None


def _repair_polygonal_loop(geoms: Any) -> tuple[np.ndarray, int]:
    """Скалярный fallback для ремонта геометрий (сохранён для обратной совместимости)."""
    result: list[Any] = []
    invalid_count = 0
    for geom in geoms:
        try:
            fixed = _repair_polygonal_single(geom)
            if fixed is None:
                invalid_count += 1
                continue
            result.append(fixed)
        except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
            invalid_count += 1
    return np.asarray(result, dtype=object), invalid_count


# ============================================================================
# ПРОСТРАНСТВЕННЫЕ ЗАПРОСЫ И ВЫЧИСЛЕНИЕ ПЛОЩАДЕЙ
# ============================================================================

def _query_tree(buf: Any, ctx: _OvertureContext) -> np.ndarray:
    """Возвращает индексы геометрий, пересекающих buf."""
    return np.asarray(
        ctx.tree.query(buf, predicate="intersects"),
        dtype=np.intp,
    )


def _compute_intersections_and_areas(
    geoms: np.ndarray,
    buf: Any,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Векторизованно вычисляет пересечения геометрий с буфером и их площади.
    Возвращает (массив пересечений, массив площадей).
    """
    intersections = shapely.intersection(geoms, buf)
    areas = shapely.area(intersections)
    mask = (areas > 0.0) & (~shapely.is_empty(intersections))
    return intersections[mask], areas[mask]


def _union_all_without_grid(geoms: Any) -> Any:
    try:
        return shapely.union_all(geoms)
    except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
        return None


def _safe_union(geoms: Any, ctx: _OvertureContext) -> Any:
    """union_all с учётом grid_size и fallback без него (или None)."""
    try:
        if ctx.union_grid_size is not None:
            return shapely.union_all(geoms, grid_size=ctx.union_grid_size)
        return shapely.union_all(geoms)
    except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
        return _union_all_without_grid(geoms)


def _union_intersections(
    valid_intersections: np.ndarray,
    ctx: _OvertureContext,
) -> Any | None:
    """
    Объединяет пересечения с учётом перекрытий, применяя chunking при необходимости.
    Возвращает объединённую геометрию или None при ошибке.
    """
    if valid_intersections.size == 0:
        return None
    if valid_intersections.size == 1:
        return valid_intersections[0]

    # Пробуем coverage_union_all, если доступно
    if ctx.use_coverage_union and hasattr(shapely, "coverage_union_all"):
        try:
            merged = shapely.coverage_union_all(valid_intersections)
            if merged is not None and not shapely.is_empty(merged):
                return merged
        except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
            pass

    # Если набор большой, обрабатываем чанками
    if valid_intersections.size > 5000:
        chunk_size = 5000
        partials = [
            _safe_union(valid_intersections[i: i + chunk_size], ctx)
            for i in range(0, valid_intersections.size, chunk_size)
        ]
        merged = _safe_union(partials, ctx)
    else:
        merged = _safe_union(valid_intersections, ctx)

    if merged is not None and not shapely.is_empty(merged):
        return merged
    return None


def _intersection_union_area_loop(
    buf: Any,
    candidate_indices: np.ndarray,
    ctx: _OvertureContext,
) -> tuple[float, bool]:
    """Скалярный fallback для вычисления площади пересечений (при ошибках векторизованного подхода)."""
    idxs = np.asarray(candidate_indices, dtype=np.intp)
    if idxs.size == 0:
        return 0.0, False

    geoms = ctx.polygon_geometries[idxs]
    intersections_list: list[Any] = []
    had_error = False

    for position, idx in enumerate(idxs):
        try:
            inter = shapely.intersection(geoms[position], buf)
            if inter is not None and not shapely.is_empty(inter) and shapely.area(inter) > 0:
                intersections_list.append(inter)
        except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError) as exc:
            had_error = True
            logger.warning(
                "Overture: ошибка intersection для здания %d: %s", int(idx), exc
            )

    if not intersections_list:
        return 0.0, had_error

    try:
        merged = shapely.union_all(intersections_list)
        if merged is not None and not shapely.is_empty(merged):
            return float(shapely.area(merged)), had_error
    except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
        logger.exception("Overture: не удалось объединить пересечения зданий")
    return 0.0, True


def _count_positive_intersections_loop(
    buf: Any,
    candidate_indices: np.ndarray,
    ctx: _OvertureContext,
) -> int:
    """Скалярно считает здания, дающие положительную площадь пересечения."""
    idxs = np.asarray(candidate_indices, dtype=np.intp)
    if idxs.size == 0:
        return 0
    geoms = ctx.polygon_geometries[idxs]
    count = 0
    for position in range(len(idxs)):
        try:
            inter = shapely.intersection(geoms[position], buf)
            if inter is not None and not shapely.is_empty(inter) and shapely.area(inter) > 0:
                count += 1
        except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
            continue
    return count


def _intersection_union_area_and_count(
    buf: Any,
    candidate_indices: np.ndarray,
    ctx: _OvertureContext,
) -> tuple[float, bool, int]:
    """Вычисляет площадь объединения и число зданий с положительной площадью пересечения."""
    idxs = np.asarray(candidate_indices, dtype=np.intp)
    if idxs.size == 0:
        return 0.0, False, 0

    try:
        candidates = ctx.polygon_geometries[idxs]
        valid_intersections, areas = _compute_intersections_and_areas(candidates, buf)
        count = int(valid_intersections.size)
        if count == 0:
            return 0.0, False, 0

        if ctx.assume_no_overlap:
            return float(areas.sum()), False, count

        if count == 1:
            return float(areas[0]), False, 1

        merged = _union_intersections(valid_intersections, ctx)
        if merged is None or shapely.is_empty(merged):
            return 0.0, False, count

        return float(shapely.area(merged)), False, count
    except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
        logger.exception("Overture: vectorized intersection failed, fallback to loop")
        area, had_error = _intersection_union_area_loop(buf, idxs, ctx)
        return area, had_error, _count_positive_intersections_loop(buf, idxs, ctx)
