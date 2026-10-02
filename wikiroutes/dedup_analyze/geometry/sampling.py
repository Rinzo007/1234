"""Approximate coverage via point sampling along lines.

Extracted from `dedup_analyze.py`: unconditional points taken uniformly
along each line; the weight of a point is length/count so that the weights
sum to the line length. Two-pass refinement restores exactness.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from shapely.errors import GEOSException

from ...dedup.constants import (
    _QUERY_CHUNK,
    MAX_POINTS_PER_LINE,
    MAX_POINTS_PER_ROUTE,
)
from ...support import safe_float as _safe_float
from .coverage import (
    _LARGE_NETWORK,
    _SpatialContext,
    _zero_cov_entries,
)


def _sample_layout(
    L: np.ndarray, step: float
) -> tuple[list[Any], list[float], list[int]]:
    """Очередь линий для сэмплирования с числом точек по каждой."""
    lines_to_sample: list[Any] = []
    lengths_to_sample: list[float] = []
    n_values: list[int] = []
    for line in L:
        if line is None or getattr(line, "is_empty", True):
            continue
        ln = _safe_float(getattr(line, "length", 0.0))
        if ln <= 0.0:
            continue
        n = max(2, min(MAX_POINTS_PER_LINE, int(np.ceil(ln / step))))
        lines_to_sample.append(line)
        lengths_to_sample.append(ln)
        n_values.append(n)
    return lines_to_sample, lengths_to_sample, n_values


def _sample_points(
    lines_to_sample: list[Any],
    lengths_to_sample: list[float],
    n_values: list[int],
    gsm: Any,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Точки и веса сэмплирования (вес = длина_линии / число_точек).

    Ограничивает суммарное число точек на маршрут: сжимает плотность,
    при экстремальном числе вариантов прореживает сам список линий.
    Возвращает ``None``, если точек построить не удалось.
    """
    total_pts = int(sum(n_values))
    if total_pts > MAX_POINTS_PER_ROUTE:
        scale = MAX_POINTS_PER_ROUTE / float(total_pts)
        n_values = [max(1, int(n * scale)) for n in n_values]
        while sum(n_values) > MAX_POINTS_PER_ROUTE and len(lines_to_sample) > 1:
            lines_to_sample = lines_to_sample[::2]
            lengths_to_sample = lengths_to_sample[::2]
            n_values = n_values[::2]
    all_pts: list[np.ndarray] = []
    weights: list[np.ndarray] = []
    for line, ln, n in zip(lines_to_sample, lengths_to_sample, n_values):
        fracs = np.linspace(0.0, 1.0, int(n))
        sampled = line.interpolate(fracs, normalized=True)
        coords = gsm.get_coordinates(sampled)
        if coords.shape[0] == 0:
            continue
        all_pts.append(coords)
        weights.append(np.full(coords.shape[0], ln / coords.shape[0]))
    if not all_pts:
        return None
    return np.concatenate(all_pts, axis=0), np.concatenate(weights, axis=0)


@dataclass(frozen=True)
class _SampleMeta:
    """Индексация партнёров для накопления покрытия (большие/малые сети)."""

    js_arr: np.ndarray
    buffer_arr: np.ndarray
    large: bool
    js_sorted: np.ndarray | None = None
    order: np.ndarray | None = None


def _sample_partner_meta(
    js_arr: np.ndarray,
    buffer_arr: np.ndarray,
    partner_sort: tuple[np.ndarray, np.ndarray] | None,
) -> _SampleMeta:
    """Сортировка партнёров для searchsorted (только большие сети, 5.6)."""
    if not (js_arr.size > 0 and len(buffer_arr) > _LARGE_NETWORK):
        return _SampleMeta(js_arr=js_arr, buffer_arr=buffer_arr, large=False)
    if partner_sort is not None:
        order, js_sorted = partner_sort
    else:
        order = np.argsort(js_arr)
        js_sorted = js_arr[order]
    return _SampleMeta(
        js_arr=js_arr,
        buffer_arr=buffer_arr,
        large=True,
        js_sorted=js_sorted,
        order=order,
    )


def _accumulate_sample(
    cov_by_j: np.ndarray,
    weights_arr: np.ndarray,
    point_idx: np.ndarray,
    global_buffer_idx: np.ndarray,
    meta: _SampleMeta,
) -> None:
    """Прибавление весов точек к покрытию партнёров по индексации meta."""
    if point_idx.size == 0:
        return
    if meta.large:
        pos = np.searchsorted(meta.js_sorted, global_buffer_idx)
        valid = (
            (pos < meta.js_sorted.size)
            & (meta.js_sorted[pos] == global_buffer_idx)
        )
        if not np.any(valid):
            return
        np.add.at(
            cov_by_j, meta.order[pos[valid]], weights_arr[point_idx[valid]]
        )
        return
    local_pos = np.full(len(meta.buffer_arr), -1, dtype=np.int64)
    local_pos[meta.js_arr] = np.arange(meta.js_arr.size, dtype=np.int64)
    local_idx = local_pos[global_buffer_idx]
    valid_mask = local_idx >= 0
    if not np.any(valid_mask):
        return
    np.add.at(
        cov_by_j,
        local_idx[valid_mask],
        weights_arr[point_idx[valid_mask]],
    )


def _sample_tree_query(
    gtree: Any,
    pts: np.ndarray,
    n_pts: int,
    weights_arr: np.ndarray,
    cov_by_j: np.ndarray,
    meta: _SampleMeta,
) -> None:
    """Запрос буферов по точкам; для плотных сетей — чанками (5.5)."""
    if n_pts > _QUERY_CHUNK:
        for s in range(0, n_pts, _QUERY_CHUNK):
            e = min(s + _QUERY_CHUNK, n_pts)
            p_idx, g_idx = gtree.query(pts[s:e], predicate="intersects")
            _accumulate_sample(cov_by_j, weights_arr, p_idx, g_idx, meta)
    else:
        p_idx, g_idx = gtree.query(pts, predicate="intersects")
        _accumulate_sample(cov_by_j, weights_arr, p_idx, g_idx, meta)


def _sample_partners_buffer(
    buffer_arr: np.ndarray, js_arr: np.ndarray
) -> list[tuple[int, Any]]:
    """Пары (локальный индекс, буфер) непустых партнёров маршрута."""
    valid: list[tuple[int, Any]] = []
    for t in range(js_arr.size):
        buf = buffer_arr[js_arr[t]]
        if buf is not None and not getattr(buf, "is_empty", True):
            valid.append((t, buf))
    return valid


def _sample_contains_fallback(
    gsm: Any,
    valid: list[tuple[int, Any]],
    pts: np.ndarray,
    weights_arr: np.ndarray,
    cov_by_j: np.ndarray,
) -> None:
    """Последовательный contains по партнёрам при сбое локального дерева."""
    for t, buf in valid:
        try:
            mask = gsm.contains(buf, pts)
        except (GEOSException, RuntimeError):
            mask = np.zeros(pts.shape[0], dtype=bool)
        cov_by_j[t] = float(
            weights_arr[np.asarray(mask, dtype=bool)].sum())


def _sample_fallback(
    gsm: Any,
    buffer_arr: np.ndarray,
    js_arr: np.ndarray,
    pts: np.ndarray,
    weights_arr: np.ndarray,
    cov_by_j: np.ndarray,
) -> None:
    """Per-route fallback: локальное дерево по партнёрам или contains."""
    valid = _sample_partners_buffer(buffer_arr, js_arr)
    if not valid:
        return
    bufs = [buf for _, buf in valid]
    idx_map_arr = np.asarray([t for t, _ in valid], dtype=np.int64)
    try:
        tree = gsm.STRtree(bufs)
        point_idx, buffer_idx = tree.query(pts, predicate="intersects")
        if point_idx.size:
            np.add.at(
                cov_by_j, idx_map_arr[buffer_idx], weights_arr[point_idx])
    except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
        _sample_contains_fallback(gsm, valid, pts, weights_arr, cov_by_j)


def _sample_coverage(
    context: _SpatialContext,
    gtree: Any,
    js_arr: np.ndarray,
    pts: np.ndarray,
    weights_arr: np.ndarray,
    cov_by_j: np.ndarray,
    partner_sort: tuple[np.ndarray, np.ndarray] | None,
) -> None:
    """Глобальный запрос STRtree по точкам, при сбое — построчный fallback."""
    n_pts = pts.shape[0]
    if n_pts > 0 and gtree is not None:
        # Векторный запрос через глобальный STRtree вместо O(m·n) вызовов
        # contains(буфер, точки); predicate "intersects" корректно учитывает
        # точки ровно на границе буфера.
        try:
            meta = _sample_partner_meta(
                js_arr, context.buffer_arr, partner_sort)
            _sample_tree_query(gtree, pts, n_pts, weights_arr, cov_by_j, meta)
        except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
            gtree = None  # принудительно уходим в per-route fallback
    if gtree is None:
        _sample_fallback(
            context.shapely_module,
            context.buffer_arr,
            js_arr,
            pts,
            weights_arr,
            cov_by_j,
        )


def _route_cov_sampled(
    context: _SpatialContext,
    i: int,
    js: list[int],
    step: float,
    gtree: Any = None,
    partner_sort: tuple[np.ndarray, np.ndarray] | None = None,
) -> list[tuple[tuple[int, int], float]]:
    """Черновой проход: покрытие по точечному сэмплированию линий.

    Точки берутся равномерно вдоль каждой линии (шаг ~ buffer_r/4); вес
    точки = длина_линии / число_точек, чтобы сумма весов равнялась длине.
    Точность восстанавливается двухпроходным пересчётом спорных пар.
    """
    js_arr = np.asarray(js, dtype=np.int64)
    L = context.route_line_arrays[i]
    lines_to_sample, lengths_to_sample, n_values = _sample_layout(L, step)
    if not n_values:
        return _zero_cov_entries(i, js)
    sampled = _sample_points(
        lines_to_sample,
        lengths_to_sample,
        n_values,
        context.shapely_module,
    )
    if sampled is None:
        return _zero_cov_entries(i, js)
    all_pts_arr, weights_arr = sampled
    pts = context.shapely_module.points(all_pts_arr)
    cov_by_j = np.zeros(js_arr.size, dtype=float)
    _sample_coverage(
        context,
        gtree,
        js_arr,
        pts,
        weights_arr,
        cov_by_j,
        partner_sort,
    )
    return [((i, j), float(cov_by_j[t])) for t, j in enumerate(js)]