"""Стадия пространственных расчётов: POI, Overture, остановки, heatmap."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..filters import compute_bbox
from ..heatmap import build_heatmap
from ..models import BBox, RouteData
from ..overture.core import compute_overture
from ..overture.load import resolve_poi_place_file
from ..poi_stops import compute_poi_stops
from ..stops import collect_unique_stops_raw

if TYPE_CHECKING:
    from ..cache import JsonCache
    from ..config import CliConfig
    from ..metrics import OvertureStats, PoiStats
    from ..report import Reporter


def bbox_as_tuple(bbox: BBox | None) -> tuple[float, float, float, float] | None:
    """Преобразует модель BBox в кортеж (min_lat, min_lon, max_lat, max_lon)."""
    if bbox is None:
        return None
    return (bbox.min_lat, bbox.min_lon, bbox.max_lat, bbox.max_lon)


@dataclass(frozen=True, slots=True)
class SpatialResult:
    """Результат стадии пространственных расчётов."""

    bbox: BBox | None
    overture_stats: dict[int, OvertureStats] = field(default_factory=dict)
    overture_meta: dict[str, Any] | None = None
    overture_dir_stats: dict[tuple[int, int], OvertureStats] = field(default_factory=dict)
    poi_stops_stats: dict[int, PoiStats] = field(default_factory=dict)
    poi_stops_meta: dict[str, Any] | None = None
    poi_stops_dir_stats: dict[tuple[int, int], PoiStats] = field(default_factory=dict)
    unique_stops: dict[str, dict[str, Any]] = field(default_factory=dict)
    heatmap: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PrecomputedPoi:
    """Результат POI-расчётов, переданный из стадии обогащения."""

    stats: dict[int, PoiStats]
    meta: dict[str, Any] | None
    dir_stats: dict[tuple[int, int], PoiStats]


def _compute_overture(
    routes: list[RouteData],
    config: CliConfig,
    bbox: tuple[float, float, float, float] | None,
    city_slug: str,
    cache: JsonCache,
    line: Callable[..., None],
) -> tuple[dict[int, OvertureStats], dict[str, Any] | None, dict[tuple[int, int], OvertureStats]]:
    """Считает POI по Overture Maps вдоль маршрутов, если включено."""
    if not config.overture:
        return {}, None, {}
    if bbox is None:
        line("  ⚠ Overture: bbox не определён — расчёт пропущен")
        return {}, None, {}
    line(f"  Overture Maps вдоль маршрутов (буфер {config.overture_buffer:.0f} м)...")
    stats, meta, dir_stats = compute_overture(
        routes,
        config.overture_file,
        config.overture_buffer,
        bbox,
        city_slug,
        cache,
        release=config.overture_release,
    )
    if stats:
        total_objects = sum(stat.count for stat in stats.values())
        line(
            f"  Рассчитано {len(stats)} маршрутов, "
            f"суммарно {total_objects} объектов"
        )
    return stats, meta, dir_stats


def _compute_poi_stops(
    routes: list[RouteData],
    config: CliConfig,
    bbox: tuple[float, float, float, float] | None,
    city_slug: str,
    cache: JsonCache,
    line: Callable[..., None],
    existing_poi: PrecomputedPoi | None,
) -> tuple[dict[int, PoiStats], dict[str, Any] | None, dict[tuple[int, int], PoiStats]]:
    """POI по остановкам: использует переданные или считает заново."""
    if existing_poi is not None:
        line(
            f"  Использованы существующие POI: "
            f"{sum(stat.count for stat in existing_poi.stats.values())} POI"
        )
        return existing_poi.stats, existing_poi.meta, existing_poi.dir_stats
    if not config.poi_stops:
        return {}, None, {}
    poi_stops_file = resolve_poi_place_file(
        None,
        config.poi_stops_file,
        bbox,
        cache.root,
        config.overture_release,
        config.overture_download_retries,
        line,
    )
    if poi_stops_file is None:
        return {}, None, {}
    line(f"  POI по остановкам (буфер {config.poi_stops_buffer:.0f} м)...")
    stats, meta, dir_stats = compute_poi_stops(
        routes,
        poi_stops_file,
        config.poi_stops_buffer,
        city_slug,
        cache,
    )
    if stats:
        total_pois = sum(stat.count for stat in stats.values())
        line(
            f"  Рассчитано {len(stats)} маршрутов, "
            f"суммарно {total_pois} POI"
        )
    return stats, meta, dir_stats


def _build_heatmap(
    unique_stops: dict[str, dict[str, Any]],
    bbox: tuple[float, float, float, float] | None,
    config: CliConfig,
) -> dict[str, Any] | None:
    """Строит heatmap плотности остановок, если он включён в конфиге."""
    if not config.heatmap:
        return None
    return build_heatmap(
        unique_stops,
        bbox,
        cell_km=config.heat_cell,
        alpha=config.heat_alpha,
        smooth=config.heat_smooth,
        gamma=config.heat_gamma,
        top_pct=config.heat_top,
    )


def run_spatial_stage(
    routes: list[RouteData],
    config: CliConfig,
    *,
    city_slug: str,
    cache: JsonCache,
    reporter: Reporter | None = None,
    existing_poi: PrecomputedPoi | None = None,
) -> SpatialResult:
    """Выполняет расчёты, зависящие от геометрии сети.

    Если передан `existing_poi`, то POI не пересчитываются, а используются
    переданные значения. Это позволяет избежать повторной загрузки и вычислений,
    если POI уже были посчитаны на стадии обогащения.
    """
    line = reporter.line if reporter is not None else lambda *_args: None

    bbox_model = compute_bbox(routes)
    bbox = bbox_as_tuple(bbox_model)

    unique_stops = collect_unique_stops_raw(routes)

    overture_stats, overture_meta, overture_dir_stats = _compute_overture(
        routes, config, bbox, city_slug, cache, line,
    )
    poi_stops_stats, poi_stops_meta, poi_stops_dir_stats = _compute_poi_stops(
        routes, config, bbox, city_slug, cache, line, existing_poi,
    )
    heatmap = _build_heatmap(unique_stops, bbox, config)

    return SpatialResult(
        bbox=bbox_model,
        overture_stats=overture_stats,
        overture_meta=overture_meta,
        overture_dir_stats=overture_dir_stats,
        poi_stops_stats=poi_stops_stats,
        poi_stops_meta=poi_stops_meta,
        poi_stops_dir_stats=poi_stops_dir_stats,
        unique_stops=unique_stops,
        heatmap=heatmap,
    )


__all__ = ["PrecomputedPoi", "SpatialResult", "bbox_as_tuple", "run_spatial_stage"]