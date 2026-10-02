"""Сборка строк пар и сводки маршрутов (dedup_analyze.pairs)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
import pandas as pd
from shapely.errors import GEOSException

from ...common import union_all
from ...dedup.geometry import (
    DedupId,
    _finite_positive_max,
)
from ...support import safe_float as _safe_float


def _build_pairs_rows(
    uniq_lo: list[int],
    uniq_hi: list[int],
    ids: list[DedupId],
    lengths: dict[DedupId, float],
    cov: dict[tuple[int, int], float],
) -> list[tuple[Any, ...]]:
    """Собирает строки пар из покрытий, нормализуя порядок (x < y)."""
    rows: list[tuple[Any, ...]] = []
    for i, j in zip(uniq_lo, uniq_hi):
        a = ids[i]
        b = ids[j]
        ca = _safe_float(cov.get((i, j), 0.0))
        cb = _safe_float(cov.get((j, i), 0.0))
        if ca <= 0.0 and cb <= 0.0:
            continue
        if a < b:
            x, y, cov_xy, cov_yx = a, b, ca, cb
            xi, yi = i, j
        else:
            x, y, cov_xy, cov_yx = b, a, cb, ca
            xi, yi = j, i
        len_x = lengths.get(x, 0.0)
        len_y = lengths.get(y, 0.0)
        k2_xy = _safe_float(cov_xy / len_x) if len_x > 0.0 else 0.0
        k2_yx = _safe_float(cov_yx / len_y) if len_y > 0.0 else 0.0
        k2_xy = min(1.0, max(0.0, k2_xy))
        k2_yx = min(1.0, max(0.0, k2_yx))
        if k2_xy == 0.0 and k2_yx == 0.0:
            continue
        rows.append((x, y, len_x, len_y, cov_xy, cov_yx, k2_xy, k2_yx, xi, yi))
    return rows


def _refine_near_pairs(
    rows: list[tuple[Any, ...]],
    ids: list[DedupId],
    merged_arr: np.ndarray,
    buffer_arr: np.ndarray,
    thr: float,
    exact_margin: float,
    shapely_module: Any,
) -> list[tuple[Any, ...]]:
    """Точный пересчёт приближённых пар в окрестности порога (2-й проход)."""
    pos = {rid: k for k, rid in enumerate(ids)}
    near = [
        (x, y, len_x, len_y, cov_xy, cov_yx, k2_xy, k2_yx, xi, yi)
        for (x, y, len_x, len_y, cov_xy, cov_yx, k2_xy, k2_yx, xi, yi) in rows
        if abs(max(k2_xy, k2_yx) - thr) <= exact_margin
        and pos.get(x) is not None
        and pos.get(y) is not None
    ]
    if not near:
        return rows
    try:
        a_idx = np.array([pos[x] for x, *_ in near], dtype=np.int64)
        b_idx = np.array([pos[y] for _, y, *_ in near], dtype=np.int64)
        # Семантика route-режима (per_direction=False): уточнение
        # спорных пар считается по объединённой геометрии ``merged``
        # (одна геометрия на направление), а не по сумме сырых вариантов.
        # Для per_direction=True это эквивалентно (одна линия = один
        # вариант). При пересекающихся вариантах merged-длина пересечения
        # может отличаться от суммы пересечений вариантов — это
        # осознанное приближение ради скорости (двухпроходный exact
        # по сырым линиям был бы точнее, но медленнее и менял бы
        # результаты относительно основного точного _route_cov).
        inter_xy = shapely_module.intersection(
            merged_arr[a_idx], buffer_arr[b_idx]
        )
        inter_yx = shapely_module.intersection(
            merged_arr[b_idx], buffer_arr[a_idx]
        )
        ex_xy = np.asarray(
            shapely_module.length(inter_xy), dtype=float)
        ex_yx = np.asarray(
            shapely_module.length(inter_yx), dtype=float)
        ex_xy = np.where(np.isfinite(ex_xy), ex_xy, 0.0)
        ex_yx = np.where(np.isfinite(ex_yx), ex_yx, 0.0)
        rebuilt_map: dict[tuple[Any, Any], tuple[Any, ...]] = {}
        for k, (x, y, len_x, len_y, _cxy, _cyx, _kx, _ky, xi, yi) in enumerate(near):
            kx = min(
                1.0, max(0.0, ex_xy[k] / len_x)) if len_x > 0.0 else 0.0
            ky = min(
                1.0, max(0.0, ex_yx[k] / len_y)) if len_y > 0.0 else 0.0
            rebuilt_map[(x, y)] = (
                x, y, len_x, len_y, float(ex_xy[k]), float(
                    ex_yx[k]), kx, ky, xi, yi
            )
        rows = [rebuilt_map.get((r[0], r[1]), r) for r in rows]
    except (GEOSException, TypeError, ValueError, AttributeError, RuntimeError):
        # Fallback: оставляем приближённые значения спорным парам.
        pass
    return rows


def _finalize_pairs(
    rows: list[tuple[Any, ...]], thr: float
) -> pd.DataFrame:
    """Строит DataFrame пар и вычисляет K2_max/Kmax/R2/RS/excess/status."""
    pairs = pd.DataFrame(
        rows,
        columns=[
            "x",
            "y",
            "len_x",
            "len_y",
            "cov_xy",
            "cov_yx",
            "K2_xy",
            "K2_yx",
            "xi",
            "yi",
        ],
    )
    if pairs.empty:
        pairs["K2_max"] = pd.Series(dtype="float64")
        pairs["Kmax"] = pd.Series(dtype="float64")
        pairs["R2"] = pd.Series(dtype="float64")
        pairs["RS"] = pd.Series(dtype="float64")
        pairs["excess"] = pd.Series(dtype="bool")
        pairs["status"] = pd.Series(dtype="string")
    else:
        k2_xy_a = pairs["K2_xy"].to_numpy()
        k2_yx_a = pairs["K2_yx"].to_numpy()
        pairs["K2_max"] = np.maximum(k2_xy_a, k2_yx_a)
        pairs["Kmax"] = pairs["K2_max"]
        max_k2 = _finite_positive_max(pairs["K2_max"])
        pairs["R2"] = pairs["K2_max"] / max_k2
        pairs["RS"] = pairs["R2"].round(3)
        pairs["excess"] = pairs["K2_max"] > thr
        pairs["status"] = np.where(pairs["excess"], "избыточно", "допустимо")
    return pairs


def _build_route_summary(
    ids: list[DedupId],
    pairs: pd.DataFrame,
    meta: dict[DedupId, dict[str, Any]],
    lines: dict[DedupId, list[Any]],
    lengths: dict[DedupId, float],
) -> tuple[
    dict[DedupId, float],
    dict[DedupId, int],
    dict[DedupId, float],
    pd.DataFrame,
]:
    """Накопленные RS-суммы, число избыточных пар и row-сводка маршрутов."""
    rs_sum: dict[DedupId, float] = dict.fromkeys(ids, 0.0)
    cnt: dict[DedupId, int] = defaultdict(int)
    if not pairs.empty:
        # Локальные индексы xi/yi уже есть в pairs — используем их напрямую,
        # без лишних dict-поисков по DirectionId. Резервный путь (нет колонок)
        # восстанавливает индексы через pos, как раньше.
        if "xi" in pairs.columns and "yi" in pairs.columns:
            xi = np.asarray(pairs["xi"].to_numpy(), dtype=np.int64)
            yi = np.asarray(pairs["yi"].to_numpy(), dtype=np.int64)
        else:
            pos = {rid: k for k, rid in enumerate(ids)}
            xi = np.array(
                [pos.get(v, -1) for v in pairs["x"].tolist()], dtype=np.int64
            )
            yi = np.array(
                [pos.get(v, -1) for v in pairs["y"].tolist()], dtype=np.int64
            )
        rs = pairs["RS"].to_numpy()
        m = pairs["excess"].to_numpy() & (xi >= 0) & (yi >= 0)
        rs_sum_arr = np.zeros(len(ids), dtype=float)
        cnt_arr = np.zeros(len(ids), dtype=np.int64)
        np.add.at(rs_sum_arr, xi[m], rs[m])
        np.add.at(rs_sum_arr, yi[m], rs[m])
        np.add.at(cnt_arr, xi[m], 1)
        np.add.at(cnt_arr, yi[m], 1)
        rs_sum = {rid: float(rs_sum_arr[k]) for k, rid in enumerate(ids)}
        cnt = {rid: int(cnt_arr[k]) for k, rid in enumerate(ids)}

    mx = max((value for value in rs_sum.values()
              if np.isfinite(_safe_float(value))), default=0.0)
    if mx <= 0.0:
        mx = 1.0
    rx = {route_id: round(_safe_float(
        rs_sum[route_id]) / mx, 3) for route_id in ids}

    summary = pd.DataFrame([
        {
            "route_id": str(route_id),
            "тип": meta.get(route_id, {}).get("type", ""),
            "directions": len(lines.get(route_id, [])),
            "length_km": round(lengths.get(route_id, 0.0) / 1000.0, 2),
            "excess_pairs": cnt.get(route_id, 0),
            "RS_sum": round(_safe_float(rs_sum.get(route_id, 0.0)), 2),
            "Rx": rx.get(route_id, 0.0),
        }
        for route_id in ids
    ])

    return rs_sum, cnt, rx, summary


def _unique_net_length(
    merged_list: list[Any],
    shapely_module: Any,
    grid_eps: float | None,
    compute_unique_net: bool,
    total_len: float,
) -> float:
    """Суммарная длина сети без перекрытий (дорогой глобальный оверлей)."""
    if compute_unique_net:
        unique_net = union_all(merged_list, shapely_module, grid_size=grid_eps)
        unique_net_len = 0.0
        if unique_net is not None and not getattr(unique_net, "is_empty", True):
            unique_net_len = _safe_float(unique_net.length)
            unique_net_len = min(unique_net_len, total_len)
    else:
        # Пропускаем оверлей: принимаем сеть полностью уникальной (коэффициент 1.0).
        unique_net_len = total_len
    return unique_net_len