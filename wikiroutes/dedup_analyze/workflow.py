"""Orchestration workflow of the overlap analysis.

Extracted from `dedup_analyze.py`: parameter validation, cache resolution
and the main `dedup_analyze` entry point.
"""
from __future__ import annotations

import os
from typing import Any

import numpy as np

from ..dedup.cache import (
    _dedup_cache_key,
    _load_dedup_cache,
    _save_dedup_cache,
)
from ..dedup.geometry import _dedup_stack
from ..models import RouteData
from ..support import safe_float as _safe_float
from .geometry import (
    _build_partners,
    _build_spatial_context,
    _candidate_pairs,
    _resolve_approximation,
    _route_cov,
    _route_cov_sampled,
    _run_cov_barrier,
    _sampling_step,
    _should_refine,
    _should_use_threads,
    _sort_partner_order,
    _SpatialContext,
)
from .pipeline import (
    _build_pairs_rows,
    _build_route_summary,
    _finalize_pairs,
    _prepare_data,
    _refine_near_pairs,
    _unique_net_length,
)


def _resolve_dedup_cache(
    routes: list[RouteData],
    buffer_r: float,
    thr: float,
    per_direction: bool,
    compute_unique_segments: bool,
    approximate: bool,
    approx_step: float | None,
    exact_margin: float,
    profile: str,
    compute_unique_net: bool,
    unique_min_m: float,
    dataset_hash: str | None,
    cache: bool,
    cache_dir: str | None,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """Возвращает (cache_dir, cache_key, сохранённый анализ) при совпадении."""
    cache_key: str | None = None
    if cache and not cache_dir:
        cache_dir = os.path.join(".", ".wikiroutes_dedup_cache")
    if cache and cache_dir:
        cache_key = _dedup_cache_key(
            routes,
            buffer_r,
            thr,
            per_direction,
            compute_unique_segments,
            approximate,
            approx_step,
            exact_margin,
            profile,
            compute_unique_net,
            unique_min_m,
            dataset_hash,
        )
        cached = _load_dedup_cache(cache_dir, cache_key)
        if cached is not None:
            return cache_dir, cache_key, cached
    return cache_dir, cache_key, None

def _validate_inputs(
    buffer_r: float,
    thr: float,
    routes: list[RouteData] | None,
) -> tuple[float, float, bool]:
    """Санитизация параметров; ``False`` — анализ не нужен (нет маршрутов)."""
    buffer_r = _safe_float(buffer_r, default=float("nan"))
    thr = _safe_float(thr, default=float("nan"))
    if not np.isfinite(buffer_r) or buffer_r < 0.0:
        raise ValueError("buffer_r должен быть неотрицательным числом")
    if not np.isfinite(thr) or not (0.0 <= thr <= 1.0):
        raise ValueError("thr должен быть числом в диапазоне [0, 1]")
    return buffer_r, thr, routes is not None

def dedup_analyze(
    routes: list[RouteData],
    buffer_r: float,
    thr: float = 0.70,
    per_direction: bool = False,
    compute_unique_segments: bool = False,
    cache: bool = False,
    cache_dir: str | None = None,
    approximate: bool = False,
    approx_step: float | None = None,
    exact_margin: float = 0.0,
    profile: str = "exact",
    compute_unique_net: bool = True,
    unique_min_m: float = 0.0,
    dataset_hash: str | None = None,
) -> dict[str, Any] | None:
    """Анализ перекрытий маршрутов.

    При ``per_direction=True`` направления идентифицируются стабильным
    ``DirectionKey = (route_id, direction_index)``. Локальные integer indices
    используются только внутри STRtree/NumPy.

    Параметры ускорения (см. оптимизации пайплайна):
        ``cache``/``cache_dir`` — сохраняет результат на диск (pickle) для
        повторных прогонов; ``approximate`` — черновой проход точечным
        сэмплированием вместо точных пересечений; ``exact_margin > 0`` включает
        двухпроходный режим (точный пересчёт только для спорных пар).
    """
    buffer_r, thr, ok = _validate_inputs(buffer_r, thr, routes)
    if not ok:
        return None

    # Кэш: при совпадении ключа возвращаем сохранённый анализ без GEOS-работы.
    cache_dir, cache_key, cached = _resolve_dedup_cache(
        routes,
        buffer_r,
        thr,
        per_direction,
        compute_unique_segments,
        approximate,
        approx_step,
        exact_margin,
        profile,
        compute_unique_net,
        unique_min_m,
        dataset_hash,
        cache,
        cache_dir,
    )
    if cached is not None:
        return cached

    stack = _dedup_stack()

    prepared = _prepare_data(routes, per_direction, buffer_r, profile, stack)
    if prepared is None:
        return None
    routes_geo, meta, lines, ids, epsg = prepared
    shapely_module = stack["shapely"]

    fast = profile == "fast"
    grid_eps = max(0.1, min(2.0, buffer_r / 50.0)) if fast else None

    # Сборка пространственного контекста: длины/bounds линий, объединённые
    # геометрии по маршруту, буфера и общий STRtree кандидатов. Отсев нулевых
    # маршрутов и обновлённые идентификаторы — внутри _build_spatial_context.
    spatial = _build_spatial_context(
        lines, ids, buffer_r, fast, grid_eps, stack["shapely"], stack["STRtree"]
    )
    if spatial is None:
        return None
    context, lengths = spatial
    ids = context.ids
    meta = {route_id: meta.get(route_id, {}) for route_id in ids}

    total_len = sum(lengths.values())
    # unique_net — глобальный union всех направлений (дорогой оверлей). Считается
    # опционально: нужен только для метрик unique_km/km_coef в этом анализе
    # (итоговые метрики сети считаются отдельно в dedup_network_after).
    unique_net_len = _unique_net_length(
        context.merged_list, shapely_module, grid_eps, compute_unique_net, total_len
    )
    km_coef = (
        round(total_len / unique_net_len, 2) if unique_net_len > 0.0 else 0.0
    )

    # Кандидаты — пары, расстояние между линиями которых <= buffer_r.
    # Это *необходимое* условие ненулевого покрытия (cov>0 <=> dist<=buffer_r),
    # тогда как пересечение буферов давало бы dist<=2*buffer_r и пропускало
    # множество пар с нулевым покрытием, заставляя зря считать пересечения.
    uniq_lo, uniq_hi = _candidate_pairs(
        context.tree, context.merged_arr, buffer_r
    )
    partners = _build_partners(uniq_lo, uniq_hi)

    # При buffer_r == 0 сэмплирование точек некорректно (важны реальные
    # пересечения/касания линий, а не попадание точек в нулевой буфер) —
    # принудительно точный расчёт; заодно строится глобальный STRtree буферов.
    approximate, buffer_tree = _resolve_approximation(
        approximate, context.buffer_arr, buffer_r, shapely_module
    )

    # Для больших сетей сортировку партнёров готовим один раз (5.6), чтобы
    # _route_cov_sampled не сортировал заново для каждого маршрута.
    partner_meta = _sort_partner_order(
        partners, buffer_tree, context.buffer_arr
    )

    # Батчинг покрытия ПО МАРШРУТАМ, а не по парам: все (линия, буфер)-комбинации
    # маршрута-источника считаются одним векторным shapely.intersection, а
    # bbox-фильтр — векторным сравнением bounds (см. _route_cov).
    cov: dict[tuple[int, int], float] = {}
    use_threads = _should_use_threads(partners, context.route_line_arrays)
    step = _sampling_step(approx_step, buffer_r)
    resolver = _build_resolver(
        approximate, step, buffer_tree, partner_meta, context
    )
    _run_cov_barrier(
        partners, context.route_line_arrays, resolver, cov, use_threads
    )

    # Только spatial layer использует локальные integer indices.
    rows = _build_pairs_rows(uniq_lo, uniq_hi, ids, lengths, cov)

    # Двухпроходный режим: приближённые пары в окрестности порога thr
    # пересчитываются точно (через точное пересечение буфера и слияния).
    # Спорные пары накапливаются и обрабатываются векторно — два GEOS-вызова
    # intersection вместо O(число_пар) последовательных вызовов.
    if _should_refine(approximate, exact_margin, buffer_r, rows):
        rows = _refine_near_pairs(
            rows,
            ids,
            context.merged_arr,
            context.buffer_arr,
            thr,
            exact_margin,
            shapely_module,
        )

    pairs = _finalize_pairs(rows, thr)

    # Векторизуем накопление RS-сумм и числа избыточных пар: вместо цикла
    # по строкам — np.add.at по локальным индексам направлений.
    _, _, rx, summary = _build_route_summary(
        ids, pairs, meta, context.lines, lengths
    )

    analysis: dict[str, Any] = {
        "ids": ids,
        "routes_geo": routes_geo,
        "meta": meta,
        "lines": context.lines,
        "lengths": lengths,
        "buffer_r": buffer_r,
        "thr": thr,
        "pairs": pairs,
        "summary": summary,
        "Rx": rx,
        "total_km": round(total_len / 1000.0, 1),
        "unique_km": round(unique_net_len / 1000.0, 1),
        "km_coef": km_coef,
        "epsg": epsg,
        "merged": context.merged,
        "grid_eps": grid_eps,
        "profile": profile,
    }

    if cache and cache_dir:
        _save_dedup_cache(cache_dir, cache_key, analysis)

    return analysis


def _build_resolver(
    approximate: bool,
    step: float,
    buffer_tree: Any,
    partner_meta: dict[int, tuple[np.ndarray, np.ndarray]],
    context: _SpatialContext,
) -> Any:
    """Фабрика резолвера покрытий: точного или чернового (сэмплирование)."""
    if approximate:
        return lambda item: _route_cov_sampled(
            context,
            item[0],
            item[1],
            step,
            buffer_tree,
            partner_meta.get(item[0]),
        )
    return lambda item: _route_cov(context, item[0], item[1])
