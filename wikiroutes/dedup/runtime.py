"""Дедупликация: высокоуровневый API.

Реализация разнесена по модулям (dedup_constants, dedup_cache, dedup_analyze,
dedup_matrix, dedup_network); этот модуль — тонкий фасад для обратной
совместимости (``from .dedup_runtime import *`` и ``from .dedup import *``).
"""
from __future__ import annotations

from ..dedup_analyze import dedup_analyze
from .matrix import materialize_dedup_matrix
from .network import dedup_network_after

__all__ = [
    "dedup_analyze",
    "dedup_network_after",
    "materialize_dedup_matrix",
]
