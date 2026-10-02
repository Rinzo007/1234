"""Публичный фасад pipeline.

Реализация вынесена в ``pipeline_runtime`` и специализированные stage-модули.
"""

from ..overture.core import compute_overture
from .dedup import apply_dedup_removals, dir_stat_volume_map
from .filtering import (
    apply_limits,
    build_filter_limits,
    split_active_routes,
    split_ok_and_errors,
)
from .loading import build_route_tasks
from .runtime import PipelineContext, PipelineResult, run_pipeline

__all__ = [
    "PipelineContext",
    "PipelineResult",
    "apply_dedup_removals",
    "apply_limits",
    "build_filter_limits",
    "build_route_tasks",
    "compute_overture",
    "dir_stat_volume_map",
    "run_pipeline",
    "split_active_routes",
    "split_ok_and_errors",
]
