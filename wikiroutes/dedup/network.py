"""Метрики сети после дедупликации (dedup_network_after)."""
from __future__ import annotations

from typing import Any

from ..common import union_all
from ..support import safe_float as _safe_float
from .geometry import DedupId, _dedup_stack

# Мемоизация dedup_network_after по (id(analysis), frozenset(active)): pipeline
# нередко вызывает метрики сети повторно для одного и того же набора active
# (например, финальная сводка), а union_all — дорогой оверлей (5.9).
_NETWORK_AFTER_CACHE: dict[tuple[int, frozenset], tuple[float, float, float]] = {}


def dedup_network_after(
    analysis: dict[str, Any],
    active: set[DedupId] | None,
) -> tuple[float, float, float]:
    """Метрики сети после дедупликации: (total_km, unique_km, km_coef).

    Учитываются только направления из ``active``; ``None`` означает «все».
    """
    if active is None:
        kept_ids = frozenset(analysis.get("ids", []))
    else:
        kept_ids = frozenset(active)
    cache_key = (id(analysis), kept_ids)
    cached = _NETWORK_AFTER_CACHE.get(cache_key)
    if cached is not None:
        return cached

    # Используем готовые merged-геометрии (одна на направление) вместо
    # повторного union_all всех сырых линий: тот же результат, но GEOS получает
    # в разы меньше входных геометрий. Сырые lines — лишь fallback, если merged
    # по какой-то причине отсутствует.
    merged_all: dict[DedupId, Any] = analysis.get("merged", {})
    if merged_all:
        kept = [geom for route_id, geom in merged_all.items() if route_id in kept_ids]
    else:
        lines: dict[DedupId, list[Any]] = analysis.get("lines", {})
        kept = [
            line
            for route_id, route_lines in lines.items()
            if route_id in kept_ids
            for line in route_lines
        ]

    total = sum(_safe_float(getattr(geom, "length", 0.0)) for geom in kept)
    unique_len = 0.0
    if kept:
        stack = _dedup_stack()
        grid_size = analysis.get("grid_eps")
        merged = union_all(kept, stack["shapely"], grid_size=grid_size)
        if merged is not None and not getattr(merged, "is_empty", True):
            unique_len = _safe_float(getattr(merged, "length", 0.0))

    total_km = round(total / 1000.0, 1)
    unique_km = round(unique_len / 1000.0, 1)
    km_coef = round(total / unique_len, 2) if unique_len > 0.0 else 0.0
    result = (total_km, unique_km, km_coef)
    if len(_NETWORK_AFTER_CACHE) > 256:
        _NETWORK_AFTER_CACHE.clear()
    _NETWORK_AFTER_CACHE[cache_key] = result
    return result

