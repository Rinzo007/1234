"""Пространственные расчёты дедупликации: покрытие и сэмплирование."""

from __future__ import annotations

from .coverage import (
    _build_partners,
    _build_spatial_context,
    _candidate_pairs,
    _resolve_approximation,
    _route_cov,
    _run_cov_barrier,
    _sampling_step,
    _should_refine,
    _should_use_threads,
    _sort_partner_order,
    _SpatialContext,
)
from .sampling import _route_cov_sampled

__all__ = [
    "_SpatialContext",
    "_build_partners",
    "_build_spatial_context",
    "_candidate_pairs",
    "_resolve_approximation",
    "_route_cov",
    "_route_cov_sampled",
    "_run_cov_barrier",
    "_sampling_step",
    "_should_refine",
    "_should_use_threads",
    "_sort_partner_order",
]