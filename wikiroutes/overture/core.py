"""Основная точка входа расчёта пересечения зданий Overture с коридорами."""

import hashlib
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import Polygon
from shapely.geometry import box as shapely_box

from .adapters import OvertureStats, utm_epsg
from .cache import LRUCache, _file_signature
from .config import OvertureConfig
from .context import _OvertureContext
from .geometry import _validate_bbox
from .load import load_overture_geometries, overture_resolve_sources
from .ports import CachePort
from .process import (
    _chunks,
    _process_single_route,
    _thread_batch_worker,
    _WorkerState,
)
from .release import resolve_overture_release
from .result import OvertureResult

logger = logging.getLogger("wikiroutes.gis.overture")


def _parse_buffer_m(value: float | None) -> float:
    if value is None or float(value) <= 0:
        raise ValueError(f"buffer_m должен быть > 0, получено {value!r}")
    return float(value)


def _coerce_buffer_m(buffer_m: float | None) -> float | None:
    try:
        return _parse_buffer_m(buffer_m)
    except ValueError as exc:
        logger.warning("Overture: некорректный buffer_m: %s", exc)
        return None


def _coerce_bbox(bbox: Any) -> tuple[float, float, float, float] | None:
    try:
        return _validate_bbox(bbox)
    except (ValueError, TypeError) as exc:
        logger.warning("Overture: некорректный bbox: %s", exc)
        return None


def _coerce_routes(routes: Any) -> list[Any] | None:
    try:
        return list(routes) if routes is not None else []
    except TypeError as exc:
        logger.warning("Overture: некорректный routes: %s", exc)
        return None


def _resolve_overture_paths(overture_path: str | None) -> list[str] | None:
    paths = overture_resolve_sources(overture_path)
    if not paths:
        logger.warning("Overture: файл не найден (--overture-file) — пропущен.")
        return None
    return paths


def _utm_project_bbox(
    bbox: tuple[float, float, float, float],
    buffer_m: float,
) -> tuple[int, tuple[float, float, float, float]] | None:
    """Проецирует bbox в UTM, расширяет его буфером; ``(epsg, qbbox)``."""
    min_lat, min_lon, max_lat, max_lon = bbox
    epsg = utm_epsg([min_lon, max_lon], [min_lat, max_lat])
    if epsg is None:
        logger.warning("Overture: не удалось определить UTM-зону, пропуск")
        return None

    from pyproj import Transformer

    bbox_poly = shapely_box(min_lon, min_lat, max_lon, max_lat)
    transformer_to_utm = Transformer.from_crs(
        "EPSG:4326", f"EPSG:{epsg}", always_xy=True
    )
    coords = np.array(bbox_poly.exterior.coords)
    tx, ty = transformer_to_utm.transform(coords[:, 0], coords[:, 1])
    bbox_utm = Polygon(np.column_stack([tx, ty]))
    bbox_utm_buffered = bbox_utm.buffer(buffer_m)

    transformer_to_wgs = Transformer.from_crs(
        f"EPSG:{epsg}", "EPSG:4326", always_xy=True
    )
    coords_buf = np.array(bbox_utm_buffered.exterior.coords)
    tx_buf, ty_buf = transformer_to_wgs.transform(
        coords_buf[:, 0], coords_buf[:, 1]
    )
    bbox_buffered_wgs = Polygon(np.column_stack([tx_buf, ty_buf]))

    min_lon_ext, min_lat_ext, max_lon_ext, max_lat_ext = bbox_buffered_wgs.bounds
    qbbox = (min_lat_ext, min_lon_ext, max_lat_ext, max_lon_ext)
    return epsg, qbbox


def _filter_buildings(
    polygon_geometries: np.ndarray, min_building_area_m2: float
) -> np.ndarray:
    """Удаляет здания площади меньше минимальной."""
    if min_building_area_m2 <= 0 or polygon_geometries.size == 0:
        return polygon_geometries
    areas = shapely.area(polygon_geometries)
    mask = areas >= min_building_area_m2
    if not mask.all():
        polygon_geometries = polygon_geometries[mask]
    del areas
    return polygon_geometries


def _pipeline_signature(
    paths: list[str],
    qbbox: tuple[float, float, float, float],
    buffer_m: float,
    limit: int,
    epsg: int,
    config: OvertureConfig,
) -> str:
    """Стабильная сигнатура контекста для кэша Overture."""
    return hashlib.sha256(
        (
            f"v={config.cache_version}|"
            f"files={_file_signature(paths)}|"
            f"bbox={tuple(float(v) for v in qbbox)}|"
            f"buffer={buffer_m:.6f}|"
            f"limit={int(limit or 0)}|"
            f"epsg={epsg}|"
            f"algorithm={config.algorithm_signature()}"
        ).encode()
    ).hexdigest()[:16]


def _build_pipeline(
    paths: list[str],
    bbox: tuple[float, float, float, float],
    buffer_m: float,
    limit: int,
    city: str,
    config: OvertureConfig,
) -> tuple[_OvertureContext, np.ndarray, int] | None:
    """Проецирует bbox, загружает и фильтрует здания, строит индекс и контекст.

    Возвращает ``(ctx, polygon_geometries, epsg)`` либо ``None`` (нет зданий
    или не удалось определить UTM-зону).
    """
    projected = _utm_project_bbox(bbox, buffer_m)
    if projected is None:
        return None
    epsg, qbbox = projected

    polygon_geometries, epsg_loaded = load_overture_geometries(
        paths, qbbox, limit, epsg, config=config
    )
    if (
        polygon_geometries is None
        or len(polygon_geometries) == 0
        or epsg_loaded is None
    ):
        logger.warning("Overture: здания не найдены или не удалось определить UTM")
        return None

    polygon_geometries = np.asarray(polygon_geometries, dtype=object)
    polygon_geometries = _filter_buildings(
        polygon_geometries, config.min_building_area_m2
    )
    if len(polygon_geometries) == 0:
        logger.warning("Overture: после фильтрации зданий не осталось")
        return None

    logger.info(
        "Overture: %d зданий, строим пространственный индекс...",
        len(polygon_geometries),
    )
    polygon_geometries = np.asarray(polygon_geometries, dtype=object)
    tree = STRtree(polygon_geometries)

    sig = _pipeline_signature(paths, qbbox, buffer_m, limit, epsg, config)

    ctx = _OvertureContext(
        polygon_geometries=polygon_geometries,
        tree=tree,
        epsg=epsg,
        buffer_m=buffer_m,
        city=city,
        sig=sig,
        config=config,
    )
    return ctx, polygon_geometries, epsg


@dataclass(slots=True)
class _BatchAccumulator:
    """Аккумулятор результатов параллельных батчей: статистика и записи кэша."""

    stats: dict[int, OvertureStats]
    dir_stats: dict[tuple[int, int], OvertureStats]
    pending: list[tuple[str, Any]]

    def add(self, route_stats, dirs, entries) -> None:
        self.stats.update(route_stats)
        self.dir_stats.update(dirs)
        if entries:
            self.pending.extend(entries.items())

    def flush_if_full(self, cache, cache_lock) -> None:
        if len(self.pending) >= 1000:
            _flush_pending(cache, cache_lock, self.pending)

    def flush(self, cache, cache_lock) -> None:
        _flush_pending(cache, cache_lock, self.pending)


def _flush_pending(
    cache: CachePort | None, cache_lock, pending: list[tuple[str, Any]]
) -> None:
    """Пачкой записывает отложенные записи кэша."""
    if cache and pending:
        with cache_lock:
            for k, v in pending:
                cache.put("overture", k, v)
        pending.clear()


def _collect_future_results(
    futures, acc: _BatchAccumulator, cache, cache_lock
) -> None:
    """Собирает результаты готовых futures в аккумулятор."""
    for future in as_completed(futures):
        try:
            route_stats, dirs, entries = future.result()
            acc.add(route_stats, dirs, entries)
            acc.flush_if_full(cache, cache_lock)
        except Exception:
            logger.exception("Overture: ошибка выполнения потокового батча")


def _effective_workers(num_workers: int | None, route_count: int) -> int:
    if num_workers is None:
        return min(os.cpu_count() or 4, route_count, 16)
    return max(1, int(num_workers))


def _thread_batch_size(
    route_count: int, num_workers: int, config: OvertureConfig
) -> int:
    if config.thread_batch_size > 0:
        return config.thread_batch_size
    auto_size = route_count // (num_workers * 4)
    return max(1, min(config.thread_batch_max, auto_size))


def _run_parallel(
    routes: list[Any],
    ctx: _OvertureContext,
    cache: CachePort | None,
    num_workers: int | None,
    buffer_cache: LRUCache,
    buffer_cache_lock: threading.RLock,
) -> tuple[dict[int, OvertureStats], dict[tuple[int, int], OvertureStats]]:
    """Параллельная потоковая обработка батчей маршрутов."""
    num_workers = _effective_workers(num_workers, len(routes))
    batch_size = _thread_batch_size(len(routes), num_workers, ctx.config)
    batches = list(_chunks(routes, batch_size))
    logger.info(
        "Overture: параллельная обработка %d маршрутов, %d потоков, %d батчей по ~%d маршрутов...",
        len(routes),
        num_workers,
        len(batches),
        batch_size,
    )
    cache_lock = threading.RLock()
    state = _WorkerState(
        ctx=ctx,
        transformer=None,
        cache=cache,
        write_cache=False,
        cache_lock=cache_lock,
        buffer_cache=buffer_cache,
        buffer_cache_lock=buffer_cache_lock,
    )
    acc = _BatchAccumulator(stats={}, dir_stats={}, pending=[])

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [
            executor.submit(_thread_batch_worker, batch, state)
            for batch in batches
        ]
        _collect_future_results(futures, acc, cache, cache_lock)
    acc.flush(cache, cache_lock)
    return acc.stats, acc.dir_stats


def _run_sequential(
    routes: list[Any],
    ctx: _OvertureContext,
    cache: CachePort | None,
    epsg: int,
    buffer_cache: LRUCache,
    buffer_cache_lock: threading.RLock,
) -> tuple[dict[int, OvertureStats], dict[tuple[int, int], OvertureStats]]:
    """Последовательная обработка маршрутов (один поток)."""
    from pyproj import Transformer

    transformer = Transformer.from_crs(
        "EPSG:4326", f"EPSG:{epsg}", always_xy=True
    )
    state = _WorkerState(
        ctx=ctx,
        transformer=transformer,
        cache=cache,
        write_cache=True,
        cache_lock=None,
        buffer_cache=buffer_cache,
        buffer_cache_lock=buffer_cache_lock,
    )
    stats: dict[int, OvertureStats] = {}
    dir_stats_map: dict[tuple[int, int], OvertureStats] = {}
    for i, rd in enumerate(routes, 1):
        route_stat, dirs, _entries = _process_single_route(rd, state)
        stats[rd.route_id] = route_stat
        dir_stats_map.update(dirs)
        if i % 50 == 0:
            logger.info("Overture: обработано %d маршрутов...", i)
    return stats, dir_stats_map


def _overture_meta(
    buffer_m: float,
    n_buildings: int,
    epsg: int,
    effective_release: str | None,
    config: OvertureConfig,
) -> dict[str, Any]:
    """Метаданные расчёта; release здесь уже разрешён на входе."""
    return {
        "buffer_m": buffer_m,
        "buildings": n_buildings,
        "epsg": epsg,
        "cache_version": config.cache_version,
        "release": effective_release,
        "shapely_version": str(shapely.__version__),
        "assume_no_overlap": config.assume_no_overlap,
        "use_coverage_union": config.use_coverage_union,
        "union_grid_size": config.union_grid_size,
        "min_building_area_m2": config.min_building_area_m2,
        "thread_batch_size": config.thread_batch_size,
        "buffer_cache_max_size": config.buffer_cache_max_size,
        "projection_chunk_size": config.projection_chunk_size,
        "algorithm_signature": config.algorithm_signature(),
    }


def compute_overture_result(
    routes: Any,
    overture_path: str | None,
    buffer_m: float | None,
    bbox: Any,
    city: str,
    cache: CachePort | None,
    limit: int = 0,
    parallel: bool = True,
    num_workers: int | None = None,
    release: str | None = None,
    config: OvertureConfig | None = None,
) -> OvertureResult[dict[int, OvertureStats]]:
    """Считает Overture и возвращает единый результат с явным статусом."""
    config = config or OvertureConfig.from_env()

    buffer_value = _coerce_buffer_m(buffer_m)
    if buffer_value is None:
        return OvertureResult.invalid_input(reason="invalid_buffer_m")

    bbox_value = _coerce_bbox(bbox)
    if bbox_value is None:
        return OvertureResult.invalid_input(reason="invalid_bbox")

    routes_value = _coerce_routes(routes)
    if routes_value is None:
        return OvertureResult.invalid_input(reason="invalid_routes")
    if not routes_value:
        return OvertureResult.skipped(reason="no_routes")

    try:
        effective_release = (
            resolve_overture_release(release)
            if release is not None
            else None
        )
    except (RuntimeError, ValueError, TypeError, AttributeError) as exc:
        logger.warning("Overture: не удалось определить release: %s", exc)
        return OvertureResult.error_result(exc)

    try:
        limit_value = int(limit or 0)
    except (TypeError, ValueError, OverflowError):
        return OvertureResult.invalid_input(reason="invalid_limit")
    if limit_value < 0:
        return OvertureResult.invalid_input(reason="invalid_limit")

    try:
        paths = _resolve_overture_paths(overture_path)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        return OvertureResult.error_result(exc)
    if paths is None:
        return OvertureResult.skipped(reason="overture_source_not_found")

    try:
        pipeline = _build_pipeline(
            paths, bbox_value, buffer_value, limit_value, city, config
        )
        if pipeline is None:
            return OvertureResult.no_data(reason="no_buildings")
        ctx, polygon_geometries, epsg = pipeline

        buffer_cache = LRUCache(max_size=config.buffer_cache_max_size)
        buffer_cache_lock = threading.RLock()

        if parallel and len(routes_value) > 1:
            stats, dir_stats_map = _run_parallel(
                routes_value,
                ctx,
                cache,
                num_workers,
                buffer_cache,
                buffer_cache_lock,
            )
        else:
            stats, dir_stats_map = _run_sequential(
                routes_value,
                ctx,
                cache,
                epsg,
                buffer_cache,
                buffer_cache_lock,
            )

        meta = _overture_meta(
            buffer_value, len(polygon_geometries), epsg, effective_release, config
        )
        failed_routes = sum(
            not stat.ok for stat in stats.values()
        )
        failed_directions = sum(
            not stat.ok for stat in dir_stats_map.values()
        )
        failed_items = failed_routes + failed_directions
        if failed_items:
            meta["status"] = "partial"
            meta["failed_items"] = failed_items
            warning = (
                f"частичный результат: {failed_items} элементов маршрутов "
                "не удалось посчитать"
            )
            return OvertureResult.partial(
                stats,
                meta,
                dir_stats_map,
                warnings=(warning,),
            )
        meta["status"] = "success"
        return OvertureResult.success(stats, meta, dir_stats_map)
    except MemoryError as exc:
        logger.warning("Overture: недостаточно памяти для обработки зданий")
        logger.exception("Overture: MemoryError")
        ctx = locals().get("ctx")
        return OvertureResult.error_result(
            exc,
            cache_signature=ctx.sig if ctx is not None else None,
        )
    except Exception as exc:
        logger.warning("Overture: ошибка при вычислении: %s", exc)
        logger.exception("Overture: исключение")
        ctx = locals().get("ctx")
        return OvertureResult.error_result(
            exc,
            cache_signature=ctx.sig if ctx is not None else None,
        )


def compute_overture(
    routes: Any,
    overture_path: str | None,
    buffer_m: float | None,
    bbox: Any,
    city: str,
    cache: CachePort | None,
    limit: int = 0,
    parallel: bool = True,
    num_workers: int | None = None,
    release: str | None = None,
    config: OvertureConfig | None = None,
) -> tuple[
    dict[int, OvertureStats],
    dict[str, Any] | None,
    dict[tuple[int, int], OvertureStats],
]:
    """Совместимый tuple-API; новый код использует compute_overture_result."""
    return compute_overture_result(
        routes=routes,
        overture_path=overture_path,
        buffer_m=buffer_m,
        bbox=bbox,
        city=city,
        cache=cache,
        limit=limit,
        parallel=parallel,
        num_workers=num_workers,
        release=release,
        config=config,
    ).as_legacy_tuple()
