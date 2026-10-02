"""Подготовка данных дедупликации: нормализация входных данных и пары."""

from __future__ import annotations

from .pairs import (
    _build_pairs_rows,
    _build_route_summary,
    _finalize_pairs,
    _refine_near_pairs,
    _unique_net_length,
)
from .prepare import _prepare_data

__all__ = [
    "_build_pairs_rows",
    "_build_route_summary",
    "_finalize_pairs",
    "_prepare_data",
    "_refine_near_pairs",
    "_unique_net_length",
]