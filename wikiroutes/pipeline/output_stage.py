"""Сборка итогового результата pipeline."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import RouteData
from .dedup_stage import DedupStageResult
from .filtering import FilterStageResult

if TYPE_CHECKING:
    from .enrichment_stage import EnrichmentResult
    from .runtime import PipelineResult
    from .spatial_stage import SpatialResult


def build_pipeline_result(
    *,
    city_slug: str,
    city_title: str,
    all_routes: list[RouteData],
    filter_result: FilterStageResult,
    enrichment: EnrichmentResult,
    dedup_result: DedupStageResult | None,
    spatial: SpatialResult,
    ok_routes: list[RouteData] | None = None,
    passenger: Any | None = None,
) -> PipelineResult:
    """Собирает публичный PipelineResult из результатов стадий.

    ``ok_routes`` — финальный набор маршрутов конвейера (после дедупликации,
    отбора вариантов линий и лимита ПОI). Если не передан, используется
    ``dedup_result.routes`` (или ``filter_result.ok_routes`` без дедупликации).
    """
    from .runtime import PipelineResult

    if ok_routes is None:
        final_routes = filter_result.ok_routes
        if dedup_result is not None:
            final_routes = dedup_result.routes
    else:
        final_routes = ok_routes

    dedup_removed = dedup_analysis = net_metrics = None
    dedup_dir_orig: dict[tuple[int, int], int] | None = None

    if dedup_result is not None:
        dedup_removed = dedup_result.removed
        dedup_analysis = dedup_result.analysis
        net_metrics = dedup_result.net_metrics
        dedup_dir_orig = dedup_result.dir_orig_index

    return PipelineResult(
        city_slug=city_slug,
        city_title=city_title,
        all_routes=all_routes,
        ok_routes=final_routes,
        bad_routes=filter_result.bad_routes,
        limits=filter_result.limits,
        excluded_routes=filter_result.excluded_routes,
        excluded_counts=filter_result.excluded_counts,
        removed_directions=filter_result.removed_directions,
        skipped_inactive=filter_result.skipped_inactive,
        bbox=spatial.bbox,
        overture_stats=spatial.overture_stats,
        overture_meta=spatial.overture_meta,
        overture_dir_stats=spatial.overture_dir_stats,
        poi_stops_stats=spatial.poi_stops_stats,
        poi_stops_meta=spatial.poi_stops_meta,
        poi_stops_dir_stats=spatial.poi_stops_dir_stats,
        dedup_removed=dedup_removed,
        dedup_analysis=dedup_analysis,
        net_metrics=net_metrics,
        dedup_dir_orig=dedup_dir_orig,
        unique_stops=spatial.unique_stops,
        heatmap=spatial.heatmap,
        boundary_geom=filter_result.boundary_geom,
        boundary_source=filter_result.boundary_source,
        passenger=passenger,
    )


__all__ = ["build_pipeline_result"]
