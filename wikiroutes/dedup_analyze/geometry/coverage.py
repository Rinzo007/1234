"""Spatial context and route coverage computation.

Extracted from `dedup_analyze.py`: batch coverage over routes; spatial
geometry arrays grouped in `_SpatialContext`; coverage functions are
modular and thread-safe (shapely 2.x ufuncs release the GIL).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import numpy as np
from shapely.errors import GEOSException

from ...common import union_all
from ...dedup.constants import (
    _COV_MAX_CELLS,
    _CPU_COUNT,
    _MAX_INTERSECTIONS,
    _THREAD_WORK,
)
from ...dedup.geometry import (
    DedupId,
    _covered_length,
)
from ...support import safe_float as _safe_float


def _run_cov_barrier(
    partners: dict[int, list[int]],
    route_line_arrays: list[np.ndarray],
    resolver: Any,
    cov: dict[tuple[int, int], float],
    use_threads: bool,
) -> None:
    """Embarrassingly-параллельный проход покрытий по маршрутам.

    При ``use_threads`` задачи сортируются по работе (линии × партнёры) и
    раздаются по потокам; иначе тот же обход в одном потоке. Порядок записи в
    ``cov`` не влияет на результаты.
    """
    if use_threads:
        tasks = sorted(
            partners.items(),
            key=lambda kv: len(route_line_arrays[kv[0]]) * len(kv[1]),
            reverse=True,
        )
        with ThreadPoolExecutor(max_workers=min(_CPU_COUNT, 8)) as ex:
            for res in ex.map(resolver, tasks):
                for key, val in res:
                    cov[key] = val
    else:
        for i, js in partners.items():
            for key, val in resolver((i, js)):
                cov[key] = val

@dataclass
class _SpatialContext:
    """Геометрия маршрутов и построенные массивы для оверлеев покрытий."""

    ids: list[DedupId]
    lines: dict[DedupId, list[Any]]
    merged: dict[DedupId, Any]
    merged_list: list[Any]
    merged_arr: np.ndarray
    tree: Any
    route_line_arrays: list[np.ndarray]
    route_line_bounds: list[np.ndarray]
    buffer_arr: np.ndarray
    buf_bounds: np.ndarray
    shapely_module: Any


_LARGE_NETWORK = 50_000


def _build_spatial_context(
    lines: dict[DedupId, list[Any]],
    orig_ids: list[DedupId],
    buffer_r: float,
    fast: bool,
    grid_eps: float | None,
    shapely_module: Any,
    STRtree: Any,
) -> tuple[_SpatialContext, dict[DedupId, float]] | None:
    """Длины/bounds линий, merged-геометрии, буфера и STRtree маршрутов.

    Отсеивает маршруты с нулевой суммарной длиной и пустой геометрией —
    после объединения вариантов должно остаться минимум два маршрута.
    """
    flat_lines: list[Any] = []
    flat_route_idx: list[int] = []
    for ri, route_id in enumerate(orig_ids):
        for line in lines[route_id]:
            flat_lines.append(line)
            flat_route_idx.append(ri)
    if flat_lines:
        flat_arr = np.asarray(flat_lines, dtype=object)
        all_lengths = np.asarray(
            shapely_module.length(flat_arr), dtype=float)
        all_lengths = np.where(np.isfinite(all_lengths), all_lengths, 0.0)
        all_bounds = np.asarray(
            shapely_module.bounds(flat_arr), dtype=float)
    else:
        flat_arr = np.empty(0, dtype=object)
        all_lengths = np.zeros(0)
        all_bounds = np.zeros((0, 4))
    line_counts = [len(lines[rid]) for rid in orig_ids]
    offsets = np.concatenate([[0], np.cumsum(line_counts)])
    raw_lengths = np.bincount(
        np.asarray(flat_route_idx, dtype=np.int64),
        weights=all_lengths,
        minlength=len(orig_ids),
    )

    # --- merged (по одной геометрии на направление) + фильтр нулевых длин ---
    merged: dict[DedupId, Any] = {}
    valid_old: list[int] = []
    for ri, route_id in enumerate(orig_ids):
        if raw_lengths[ri] <= 0.0:
            continue
        route_lines = lines[route_id]
        # Одиночная линия уже «объединена» — union_all только для наборов.
        geom = (
            union_all(route_lines, shapely_module, grid_size=grid_eps)
            if len(route_lines) > 1
            else route_lines[0]
        )
        if geom is None or getattr(geom, "is_empty", True):
            continue
        merged[route_id] = geom
        valid_old.append(ri)
    ids = [orig_ids[ri] for ri in valid_old]
    if len(ids) < 2:
        return None

    lines = {route_id: lines[route_id] for route_id in ids}
    lengths = {
        route_id: float(raw_lengths[ri]) for route_id, ri in zip(ids, valid_old)
    }

    # Пер-направленческие массивы линий и bounds — для векторного bbox-фильтра
    # и батчинга пересечений (один C-вызов на сеть, а не по парам).
    route_line_arrays: list[np.ndarray] = [
        flat_arr[offsets[o]:offsets[o + 1]] for o in valid_old
    ]
    route_line_bounds: list[np.ndarray] = [
        all_bounds[offsets[o]:offsets[o + 1]] for o in valid_old
    ]

    merged_list = [merged[route_id] for route_id in ids]
    merged_arr = np.asarray(merged_list, dtype=object)
    # Буферизация и bounds буферов — по одному C-вызову; buf_bounds избавляет
    # от повторного .bounds буфера внутри каждого пересечения (2*P раз).
    if buffer_r > 0.0:
        if fast:
            # Меньше сегментов окружности (quad_segs) и mitre-стыки — меньше
            # вершин. Круглые торцы (cap_style) сохраняем: плоские торцы
            # зануляют покрытие в общих точках маршрутов.
            buffer_arr = shapely_module.buffer(
                merged_arr, buffer_r, quad_segs=2, join_style=2
            )
        else:
            buffer_arr = shapely_module.buffer(
                merged_arr, buffer_r, quad_segs=4)
    else:
        buffer_arr = merged_arr
    buf_bounds = np.asarray(
        shapely_module.bounds(buffer_arr), dtype=float)

    context = _SpatialContext(
        ids=ids,
        lines=lines,
        merged=merged,
        merged_list=merged_list,
        merged_arr=merged_arr,
        tree=STRtree(merged_list),
        route_line_arrays=route_line_arrays,
        route_line_bounds=route_line_bounds,
        buffer_arr=buffer_arr,
        buf_bounds=buf_bounds,
        shapely_module=shapely_module,
    )
    return context, lengths

def _zero_cov_entries(i: int, js: list[int]) -> list[tuple[tuple[int, int], float]]:
    """Нулевые покрытия маршрута по всем партнёрам (ранний выход)."""
    return [((i, j), 0.0) for j in js]


def _bbox_hits(
    buf_bounds: np.ndarray, js_arr: np.ndarray, B: np.ndarray
) -> np.ndarray:
    """Индексы партнёров, чьи буфера перекрывают bbox линий маршрута."""
    bb_all = buf_bounds[js_arr]
    B_minx = float(B[:, 0].min())
    B_maxx = float(B[:, 2].max())
    B_miny = float(B[:, 1].min())
    B_maxy = float(B[:, 3].max())
    possible = (
        (bb_all[:, 2] >= B_minx)
        & (bb_all[:, 0] <= B_maxx)
        & (bb_all[:, 3] >= B_miny)
        & (bb_all[:, 1] <= B_maxy)
    )
    return np.nonzero(possible)[0]


def _route_cov_chunk(
    gsm: Any,
    L: np.ndarray,
    B: np.ndarray,
    js_chunk: np.ndarray,
    buf_bounds: np.ndarray,
    buffer_arr: np.ndarray,
) -> np.ndarray:
    """Покрытия линий маршрута по партнёрам чанка (векторный оверлей).

    Чанкует пересечения на куски _MAX_INTERSECTIONS, чтобы не порождать
    слишком больших GEOS-массивов на плотных bbox-матрицах.
    """
    cov = np.zeros(js_chunk.size, dtype=float)
    bb_chunk = buf_bounds[js_chunk]
    ov = (
        (B[:, 2:3] >= bb_chunk[:, 0][None, :])
        & (bb_chunk[:, 2][None, :] >= B[:, 0:1])
        & (B[:, 3:4] >= bb_chunk[:, 1][None, :])
        & (bb_chunk[:, 3][None, :] >= B[:, 1:2])
    )
    kk, mm = np.nonzero(ov)
    if kk.size:
        for s in range(0, kk.size, _MAX_INTERSECTIONS):
            e = min(s + _MAX_INTERSECTIONS, kk.size)
            bufs = buffer_arr[js_chunk[mm[s:e]]]
            inter = gsm.intersection(L[kk[s:e]], bufs)
            lens = np.asarray(gsm.length(inter), dtype=float)
            lens = np.where(np.isfinite(lens), lens, 0.0)
            cov += np.bincount(
                mm[s:e], weights=lens, minlength=js_chunk.size)
    return cov


def _fallback_cov_entries(
    context: _SpatialContext, i: int, js: list[int]
) -> list[tuple[tuple[int, int], float]]:
    """Построчный fallback покрытий при сбое оверлея."""
    return [
        (
            (i, j),
            _safe_float(
                _covered_length(
                    context.lines[context.ids[i]],
                    context.buffer_arr[j],
                    context.shapely_module,
                    context.route_line_bounds[i],
                )
            ),
        )
        for j in js
    ]


def _route_cov(
    context: _SpatialContext, i: int, js: list[int]
) -> list[tuple[tuple[int, int], float]]:
    """Покрытые длины маршрута-источника i по всем его партнёрам.

    Вычисляется одним векторным shapely.intersection (с векторным
    bbox-фильтром). Безопасно вызывать из другого потока: shapely 2.x
    ufunc'ы отпускают GIL и потокобезопасны для независимых геометрий.
    """
    js_arr = np.asarray(js, dtype=np.int64)
    L = context.route_line_arrays[i]
    B = context.route_line_bounds[i]
    possible_idx = _bbox_hits(context.buf_bounds, js_arr, B)
    if possible_idx.size == 0:
        return _zero_cov_entries(i, js)
    try:
        # Чанкуем партнёров, чтобы bbox-матрица (K × m) не раздувалась по
        # памяти на маршрутах с большим числом вариантов и партнёров.
        cov_work = np.zeros(possible_idx.size, dtype=float)
        chunk_size = max(1, _COV_MAX_CELLS // max(1, len(L)))
        for start in range(0, possible_idx.size, chunk_size):
            end = min(start + chunk_size, possible_idx.size)
            js_chunk = js_arr[possible_idx[start:end]]
            cov_work[start:end] = _route_cov_chunk(
                context.shapely_module,
                L,
                B,
                js_chunk,
                context.buf_bounds,
                context.buffer_arr,
            )
        cov_by_j = np.zeros(js_arr.size, dtype=float)
        cov_by_j[possible_idx] = cov_work
        out: list[tuple[tuple[int, int], float]] = []
        for t, j in enumerate(js):
            out.append(((i, j), float(cov_by_j[t])))
    except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
        return _fallback_cov_entries(context, i, js)
    return out

def _candidate_pairs(
    tree: Any,
    merged_arr: np.ndarray,
    buffer_r: float,
) -> tuple[list[int], list[int]]:
    """Пара кандидатов (lo, hi) с расстоянием между линиями <= buffer_r."""
    candidates = tree.query(
        merged_arr, predicate="dwithin", distance=buffer_r)
    lower = np.minimum(candidates[0], candidates[1])
    upper = np.maximum(candidates[0], candidates[1])
    different = lower != upper
    lo = lower[different]
    hi = upper[different]
    if lo.size:
        # Кодируем пару в int64 (lo*n + hi) и берём unique за O(N log N)
        # вместо np.unique по 2D-массиву.
        n_ids = len(merged_arr)
        code = lo.astype(np.int64) * np.int64(n_ids) + hi.astype(np.int64)
        uniq = np.unique(code)
        uniq_lo = (uniq // n_ids).tolist()
        uniq_hi = (uniq % n_ids).tolist()
    else:
        uniq_lo, uniq_hi = [], []
    return uniq_lo, uniq_hi


def _build_partners(
    uniq_lo: list[int], uniq_hi: list[int]
) -> dict[int, list[int]]:
    """Симметричный словарь партнёров (i -> списки j) для batch-покрытий."""
    partners: dict[int, list[int]] = {}
    for i, j in zip(uniq_lo, uniq_hi):
        partners.setdefault(i, []).append(j)
        partners.setdefault(j, []).append(i)
    return partners


def _resolve_approximation(
    approximate: bool,
    buffer_arr: np.ndarray,
    buffer_r: float,
    shapely_module: Any,
) -> tuple[bool, Any]:
    """При buffer_r == 0 сэмплирование некорректно — переключаемся на exact.

    Возвращает (approximate, глобальный STRtree буферов или None). Дерево
    строится один раз и переиспользуется во всех вызовах _route_cov_sampled
    вместо построения дерева на партнёрах каждого маршрута. Пустые буфера
    не дадут совпадений; при сбое построения — построчный fallback.
    """
    if approximate and buffer_r <= 0.0:
        approximate = False
    buffer_tree = None
    if approximate:
        try:
            buffer_tree = shapely_module.STRtree(buffer_arr)
        except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
            buffer_tree = None
    return approximate, buffer_tree


def _should_use_threads(
    partners: dict[int, list[int]],
    route_line_arrays: list[np.ndarray],
) -> bool:
    """Вход в многопоточность по реальной работе (линии × партнёры)."""
    total_work = sum(
        len(route_line_arrays[i]) * len(js) for i, js in partners.items()
    )
    return (
        _CPU_COUNT > 1
        and partners
        and (
            total_work >= _THREAD_WORK
            or any(
                len(route_line_arrays[i]) * len(js) >= _THREAD_WORK
                for i, js in partners.items()
            )
        )
    )


def _sort_partner_order(
    partners: dict[int, list[int]],
    buffer_tree: Any,
    buffer_arr: np.ndarray,
) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    """Сортировка партнёров впрок для больших сетей (ускоряет searchsorted)."""
    partner_meta: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    if buffer_tree is not None and len(buffer_arr) > _LARGE_NETWORK:
        for i, js in partners.items():
            js_arr_i = np.asarray(js, dtype=np.int64)
            order = np.argsort(js_arr_i)
            partner_meta[i] = (order, js_arr_i[order])
    return partner_meta


def _sampling_step(approx_step: float | None, buffer_r: float) -> float:
    """Шаг сэмплирования (~ buffer_r/4), если не задан явно."""
    if approx_step and approx_step > 0.0:
        return approx_step
    return max(buffer_r / 4.0, 1.0)


def _should_refine(
    approximate: bool,
    exact_margin: float,
    buffer_r: float,
    rows: list[Any],
) -> bool:
    """Точный пересчёт нужен только в двухпроходном (approximate) режиме."""
    return (
        approximate
        and exact_margin > 0.0
        and buffer_r > 0.0
        and bool(rows)
    )
