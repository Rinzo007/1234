"""Вспомогательные функции дедупликации: идентификаторы направлений,
индексы пар анализа и подсчёт самоперекрывающихся пар.

Направления удерживаются жадно по убыванию весов POI; при равном весе
сохраняется направление с большим стабильным ID. Инвариант маршрута —
не более двух направлений — enforced в ``compute.enforce_route_limits``.

Осознанное продуктовое решение (аудит F6): пары направлений ВНУТРИ одного
маршрута — например, «туда»/«обратно» кольцевого маршрута с идентичной
геометрией — считаются дубликатами наравне с парами разных маршрутов.
Такое направление удаляется по общим правилам (минимальный вес, при равном
весе — меньший ID). Число таких схлопнутых пар доступно через
:func:`count_self_redundant_pairs` и ``net_metrics["self_redundant_pairs"]``
— это делает семантику видимой и фиксирует её для будущих изменений.

Ограничение F6: направлений у маршрута остаётся не более двух. Пара «туда»
(``di == 0``)/«обратно» (``di == 1``) неразделима: если удалено лишь одно из
двух, а парное осталось активным, удаление откатывается. Поэтому такая пара
схлопывается только при удалении обоих направлений; иначе она остаётся в
остатке (``residual``). Лишние направления (``di >= 2``) удаляются.
"""

from __future__ import annotations

from typing import Any, TypeAlias

import numpy as np

from ..support import safe_float as _safe_float
from ..type_defs import DirectionKey

DirectionId: TypeAlias = int | DirectionKey


def _id_sort_key(value: DirectionId) -> tuple[int, int, int]:
    if isinstance(value, tuple) and len(value) == 2:
        return 0, int(value[0]), int(value[1])
    return 1, int(value), -1


def _heap_key(
    route_id: DirectionId,
    length: float,
    max_k: float,
    generation: int,
) -> tuple[float, float, int, int, int, DirectionId, int]:
    id_kind, id_a, id_b = _id_sort_key(route_id)
    # min-heap: жертвой выталкивается маршрут с наименьшей длиной; при равной
    # длине — с большим Kmax, затем с меньшим стабильным ID.
    return length, -max_k, id_kind, id_a, id_b, route_id, generation


def _route_key(value: DirectionId) -> tuple[int, int]:
    if isinstance(value, tuple) and len(value) == 2:
        return int(value[0]), int(value[1])
    return int(value), -1


def _coerce_direction_id(value: Any) -> DirectionId | None:
    if isinstance(value, tuple) and len(value) == 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError, OverflowError):
            return None
    scalar: Any = value
    try:
        return int(scalar)
    except (TypeError, ValueError, OverflowError):
        return None


def _pair_indices(
    base_pairs: Any,
    ids_list: list[DirectionId],
) -> tuple[np.ndarray, np.ndarray, dict[DirectionId, int]]:
    id_to_idx: dict[DirectionId, int] = {rid: k for k, rid in enumerate(ids_list)}
    if (
        getattr(base_pairs, "empty", True)
        or "xi" not in getattr(base_pairs, "columns", [])
        or "yi" not in getattr(base_pairs, "columns", [])
    ):
        xs = base_pairs["x"].to_numpy()
        ys = base_pairs["y"].to_numpy()
        x_idx = np.array(
            [id_to_idx.get(_coerce_direction_id(v), -1) for v in xs],
            dtype=np.int64,
        )
        y_idx = np.array(
            [id_to_idx.get(_coerce_direction_id(v), -1) for v in ys],
            dtype=np.int64,
        )
        return x_idx, y_idx, id_to_idx
    x_idx = np.asarray(base_pairs["xi"].to_numpy(), dtype=np.int64)
    y_idx = np.asarray(base_pairs["yi"].to_numpy(), dtype=np.int64)
    return x_idx, y_idx, id_to_idx


def count_self_redundant_pairs(
    analysis: dict[str, Any],
    k_del: float,
) -> int:
    base_pairs = analysis.get("pairs")
    if base_pairs is None or getattr(base_pairs, "empty", True):
        return 0
    if "Kmax" not in getattr(base_pairs, "columns", []):
        return 0

    thr = _safe_float(k_del, default=float("nan"))
    if not np.isfinite(thr):
        return 0

    kmax = np.asarray(base_pairs["Kmax"], dtype=float).ravel()
    ids_list = list(analysis.get("ids", []))
    if (
        ids_list
        and "xi" in getattr(base_pairs, "columns", [])
        and "yi" in getattr(base_pairs, "columns", [])
    ):
        x_idx = np.asarray(base_pairs["xi"].to_numpy(), dtype=np.int64)
        y_idx = np.asarray(base_pairs["yi"].to_numpy(), dtype=np.int64)
        n_ids = len(ids_list)
        ok = (
            (x_idx != y_idx)
            & (x_idx >= 0)
            & (y_idx >= 0)
            & (x_idx < n_ids)
            & (y_idx < n_ids)
        )
        if not ok.any():
            return 0
        route_of = np.array(
            [_route_key(rid)[0] for rid in ids_list], dtype=np.int64
        )
        x_routes = route_of[x_idx[ok]]
        y_routes = route_of[y_idx[ok]]
        same_route = (x_routes == y_routes) & (kmax[ok] > thr)
        return int(np.count_nonzero(same_route))
    else:
        xs = base_pairs["x"].to_numpy()
        ys = base_pairs["y"].to_numpy()
        x_ids = np.fromiter(
            (_coerce_direction_id(v) for v in xs), dtype=object, count=len(xs)
        )
        y_ids = np.fromiter(
            (_coerce_direction_id(v) for v in ys), dtype=object, count=len(ys)
        )
        ok = (x_ids != None) & (y_ids != None) & (x_ids != y_ids)
        x_routes = np.fromiter(
            (_route_key(v)[0] if v is not None else -1 for v in x_ids),
            dtype=np.int64,
            count=len(xs),
        )
        y_routes = np.fromiter(
            (_route_key(v)[0] if v is not None else -1 for v in y_ids),
            dtype=np.int64,
            count=len(ys),
        )
    same_route = ok & (x_routes == y_routes) & (kmax > thr)
    return int(np.count_nonzero(same_route))


__all__ = [
    "DirectionId",
    "_coerce_direction_id",
    "_heap_key",
    "_id_sort_key",
    "_pair_indices",
    "_route_key",
    "count_self_redundant_pairs",
]
