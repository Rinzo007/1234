"""Стадия обогащения: POI вдоль остановок."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..filters import compute_bbox
from ..metrics import PoiStats
from ..models import RouteData
from ..overture.load import resolve_poi_place_file
from ..poi_stops import compute_poi_stops

if TYPE_CHECKING:
    from ..cache import JsonCache
    from ..config import CliConfig
    from ..report import Reporter


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    """Результат стадии обогащения."""

    poi_stops_stats: dict[int, PoiStats]
    poi_stops_meta: dict[str, Any] | None
    poi_stops_dir_stats: dict[tuple[int, int], PoiStats]


def _resolve_bbox(
    routes: list[RouteData],
    bbox: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    """Использует переданный bbox или вычисляет его из маршрутов."""
    if bbox is not None:
        return bbox
    bbox_model = compute_bbox(routes)
    if bbox_model is None:
        return None
    return (
        bbox_model.min_lat,
        bbox_model.min_lon,
        bbox_model.max_lat,
        bbox_model.max_lon,
    )


def _log_stats(line: Any, stats: dict[int, Any], total: str) -> None:
    """Отчёт о рассчитанных маршрутах (если они есть)."""
    if stats:
        line(f"  Рассчитано {len(stats)} маршрутов, суммарно {total}")


def _resolve_poi_file(
    bbox: tuple[float, float, float, float] | None,
    poi_file_override: str | None,
    config: CliConfig,
    cache: JsonCache,
    line: Any,
) -> str | None:
    """Файл POI: переопределение → конфиг → автозагрузка Overture place."""
    return resolve_poi_place_file(
        poi_file_override,
        config.poi_stops_file,
        bbox,
        cache.root,
        config.overture_release,
        config.overture_download_retries,
        line,
    )


def _compute_poi_block(
    routes: list[RouteData],
    config: CliConfig,
    city_slug: str,
    cache: JsonCache,
    line: Any,
    bbox: tuple[float, float, float, float] | None,
    poi_file_override: str | None,
) -> tuple[dict[int, PoiStats], dict[str, Any] | None, dict[tuple[int, int], PoiStats]]:
    """POI вдоль остановок."""
    stats: dict[int, PoiStats] = {}
    meta: dict[str, Any] | None = None
    dir_stats: dict[tuple[int, int], PoiStats] = {}
    if not config.poi_stops:
        return stats, meta, dir_stats

    poi_stops_file = _resolve_poi_file(bbox, poi_file_override, config, cache, line)
    if poi_stops_file is None:
        return stats, meta, dir_stats

    line(f"  POI-stops вдоль остановок (буфер {config.poi_stops_buffer:.0f} м)...")
    stats, meta, dir_stats = compute_poi_stops(
        routes,
        poi_stops_file,
        config.poi_stops_buffer,
        city_slug,
        cache,
    )
    total = sum(stat.count for stat in stats.values())
    _log_stats(line, stats, f"{total} POI")
    return stats, meta, dir_stats


def run_enrichment_stage(
    routes: list[RouteData],
    config: CliConfig,
    *,
    city_slug: str,
    cache: JsonCache,
    bbox: tuple[float, float, float, float] | None = None,
    reporter: Reporter | None = None,
    poi_file_override: str | None = None,
) -> EnrichmentResult:
    """Считает POI вокруг остановок.

    Если `bbox` не передан, вычисляется из маршрутов.
    Если передан `poi_file_override`, он используется вместо `config.poi_stops_file`
    для POI-расчётов, что позволяет избежать повторной автозагрузки.
    """
    line = reporter.line if reporter is not None else lambda *_args: None
    bbox = _resolve_bbox(routes, bbox)

    poi_stops_stats, poi_stops_meta, poi_stops_dir_stats = _compute_poi_block(
        routes, config, city_slug, cache, line, bbox, poi_file_override
    )

    return EnrichmentResult(
        poi_stops_stats=poi_stops_stats,
        poi_stops_meta=poi_stops_meta,
        poi_stops_dir_stats=poi_stops_dir_stats,
    )


__all__ = ["EnrichmentResult", "run_enrichment_stage"]