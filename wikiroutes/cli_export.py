"""Экспортные операции CLI.

Содержит orchestration вокруг XLSX/KML/heatmap. Тяжёлая бизнес-логика
самих форматов остаётся в соответствующих модулях.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from .config import CliConfig
from .enums import RouteType
from .geojson_export import build_boundary_geojson, build_terminals_geojson
from .heatmap import build_heatmap_kml
from .kml import build_kml, build_map_dedup_kml
from .models import RouteData
from .pipeline.runtime import PipelineResult
from .pipeline.spatial_stage import bbox_as_tuple
from .report import render_errors
from .xlsx import build_xlsx


def output_base_name(
    output: str | None,
    city_slug: str,
    type_filter: Iterable[RouteType] | None = None,
) -> str:
    """Возвращает безопасное базовое имя выходных файлов.

    Если задан конкретный ``--type``, к авто-имени добавляется суффикс со
    списком типов (``voronezh_tram`` / ``voronezh_bus_tram``), чтобы выгрузки
    по разным видам транспорта не перезаписывали друг друга. Явно заданное
    через ``--output`` имя не модифицируется.
    """
    safe_city_name = (
        re.sub(r"[^a-z0-9_-]+", "_", city_slug.lower()).strip("_") or "city"
    )
    if output:
        base = output
    elif type_filter:
        types_suffix = "_".join(sorted(t.value for t in type_filter))
        base = f"{safe_city_name}_{types_suffix}"
    else:
        base = f"{safe_city_name}_routes"
    return re.sub(r"\.(xlsx|kml)$", "", base, flags=re.IGNORECASE)


def write_xlsx(
    config: CliConfig,
    result: PipelineResult,
    kml_routes: list[RouteData],
    output_base: str,
) -> None:
    xlsx_path = build_xlsx(
        config,
        result,
        kml_routes,
        output_base + ".xlsx",
    )
    if xlsx_path:
        print(f"  ✅ XLSX : {xlsx_path}")


def write_kml(
    config: CliConfig,
    result: PipelineResult,
    kml_routes: list[RouteData],
    output_base: str,
) -> None:
    kml_path = build_kml(
        kml_routes,
        result.city_title,
        output_base + ".kml",
        include_stops=config.stops,
        bbox=bbox_as_tuple(result.bbox),
        dir_orig=result.dedup_dir_orig,
        overture_stats=result.overture_stats,
        overture_meta=result.overture_meta,
        overture_dir_stats=result.overture_dir_stats,
    )
    if kml_path:
        print(f"  ✅ KML  : {kml_path}  (маршрутов: {len(kml_routes)})")


def write_heatmap(config: CliConfig, result: PipelineResult, output_base: str) -> None:
    if not result.heatmap:
        return
    path = build_heatmap_kml(
        result.heatmap,
        result.city_title,
        output_base + "_heatmap.kml",
        max_height=config.heat_max_height,
        flat=config.heat_flat,
    )
    if path:
        print(f"  ✅ KML heatmap: {path} (ячеек: {len(result.heatmap['cells'])})")


def write_terminals_geojson(
    config: CliConfig,
    result: PipelineResult,
    output_base: str,
) -> None:
    path = build_terminals_geojson(
        result.ok_routes,
        result.city_title,
        output_base + "_terminals.geojson",
        bbox=bbox_as_tuple(result.bbox),
    )
    if path:
        print(f"  ✅ GeoJSON остановок: {path}")


def write_boundary_geojson(
    config: CliConfig,
    result: PipelineResult,
    output_base: str,
) -> None:
    boundary = result.boundary_geom
    source = result.boundary_source

    if source == "Overture-buildings" and boundary is not None:
        from .urban_boundary import boundary_bbox

        boundary = boundary_bbox(boundary)

    path = build_boundary_geojson(
        boundary,
        source,
        result.city_title,
        output_base + "_boundary.geojson",
    )
    if path:
        print(f"  ✅ GeoJSON границы города: {path}")


def write_map_dedup(
    config: CliConfig,
    result: PipelineResult,
    kml_routes: list[RouteData],
    output_base: str,
) -> None:
    if not config.map_type:
        return
    path = build_map_dedup_kml(
        kml_routes,
        result.all_routes,
        result.dedup_removed,
        result.city_title,
        output_base + f"_{config.map_type.value}_dedup.kml",
        config.map_type.value,
        dir_orig=result.dedup_dir_orig,
    )
    if path:
        print(f"  ✅ KML карта {config.map_type.value}: {path}")


def export_outputs(config: CliConfig, result: PipelineResult) -> None:
    print("\n[4/4] Генерация выводов...")
    kml_routes = result.ok_routes
    base = output_base_name(config.output, result.city_slug, config.type_filter)

    if "xlsx" in config.output_formats:
        write_xlsx(config, result, kml_routes, base)
    if "kml" in config.output_formats:
        write_kml(config, result, kml_routes, base)
    write_map_dedup(config, result, kml_routes, base)
    write_heatmap(config, result, base)
    if config.terminals_geojson:
        write_terminals_geojson(config, result, base)
    if config.boundary_geojson:
        write_boundary_geojson(config, result, base)
    errors_text = render_errors(result.bad_routes)
    if errors_text:
        print(f"\n{errors_text}")


__all__ = [
    "export_outputs",
    "output_base_name",
    "write_boundary_geojson",
    "write_heatmap",
    "write_kml",
    "write_map_dedup",
    "write_terminals_geojson",
    "write_xlsx",
]