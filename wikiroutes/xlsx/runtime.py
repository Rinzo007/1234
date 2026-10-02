"""Оркестрация XLSX-экспорта.

Конкретные листы отчёта находятся в специализированных модулях
``xlsx_summary``, ``xlsx_routes``, ``xlsx_dedup`` и
``xlsx_simple``. Этот модуль отвечает только за создание книги и порядок
вызова writer-функций.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..models import RouteData
from .dedup import write_dedup_sheet
from .helpers import network_density
from .passenger import write_passenger_sheet
from .routes import write_routes_sheet
from .simple import (
    write_errors_sheet,
    write_heatmap_sheet,
    write_removed_directions_sheet,
    write_unique_stops_sheet,
)
from .summary import summary_rows, write_summary_sheet

logger = logging.getLogger("wikiroutes.xlsx")


def build_xlsx_workbook(
    routes: Sequence[RouteData],
    city: str,
    city_title: str,
    path: str | Path,
    curv_limit: float = 0.0,
    minlen_limit: float = 0.0,
    maxlen_limit: float = 0.0,
    radius_limit: float = 0.0,
    cut_curv: int = 0,
    cut_len: int = 0,
    cut_radius: int = 0,
    removed_direction_routes: int = 0,
    fully_excluded_routes: int = 0,
    skipped_inactive: int = 0,
    kml_routes: Sequence[RouteData] | None = None,
    unique_stops: Mapping[str, Mapping[str, Any]] | None = None,
    excluded_stage2: Sequence[tuple[RouteData, str]] | None = None,
    removed_directions: Sequence[tuple[RouteData, Any, str]] | None = None,
    heatmap: Mapping[str, Any] | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    poi_stops_stats: Mapping[Any, Any] | None = None,
    poi_stops_meta: Mapping[str, Any] | None = None,
    poi_stops_dir_stats: Mapping[Any, Any] | None = None,
    dedup_removed: Sequence[Mapping[str, Any]] | None = None,
    dedup_analysis: Mapping[str, Any] | None = None,
    net_metrics: Mapping[str, Any] | None = None,
    overture_stats: Mapping[Any, Any] | None = None,
    overture_meta: Mapping[str, Any] | None = None,
    overture_dir_stats: Mapping[Any, Any] | None = None,
    passenger: Any = None,
) -> str | Path | None:
    """Создаёт XLSX-отчёт и возвращает путь к нему."""
    try:
        from openpyxl import Workbook
    except ImportError:
        logger.warning("XLSX пропущен: pip install openpyxl")
        return None

    ok = [route for route in routes if not route.error and route.directions]
    bad = [route for route in routes if route.error]
    ideas = [route for route in routes if route.is_idea and not route.error]

    fully_excluded_routes = len(excluded_stage2) if excluded_stage2 else 0
    removed_direction_routes = 0
    if removed_directions:
        removed_ids = {(r.route_id, r.route_type) for r, _, _ in removed_directions}
        fully_excluded_ids = (
            {(r.route_id, r.route_type) for r, _ in excluded_stage2}
            if excluded_stage2
            else set()
        )
        removed_direction_routes = len(removed_ids - fully_excluded_ids)

    wb = Workbook()

    # Извлекаем буфер POI из метаданных, если они есть
    poi_buffer_m = 0
    if poi_stops_meta and isinstance(poi_stops_meta, dict):
        poi_buffer_m = poi_stops_meta.get("buffer_m", 0)

    write_summary_sheet(
        wb,
        summary_rows(
            city=city,
            city_title=city_title,
            routes=routes,
            ok=ok,
            bad=bad,
            ideas=ideas,
            skipped_inactive=skipped_inactive,
            curv_limit=curv_limit,
            minlen_limit=minlen_limit,
            maxlen_limit=maxlen_limit,
            radius_limit=radius_limit,
            cut_curv=cut_curv,
            cut_len=cut_len,
            cut_radius=cut_radius,
            removed_direction_routes=removed_direction_routes,
            fully_excluded_routes=fully_excluded_routes,
            dedup_removed=dedup_removed,
            dedup_analysis=dedup_analysis,
            net_metrics=net_metrics,
            net_density=network_density(net_metrics, bbox),
            kml_routes=kml_routes,
            unique_stops=unique_stops,
            poi_stops_stats=poi_stops_stats,
            overture_stats=overture_stats,
            overture_meta=overture_meta,
            poi_buffer_m=poi_buffer_m,
            poi_stats=poi_stops_stats,
        ),
    )

    if kml_routes:
        write_routes_sheet(
            wb,
            city=city,
            kml_routes=kml_routes,
            overture_stats=overture_stats,
            overture_dir_stats=overture_dir_stats,
            poi_stops_stats=poi_stops_stats,
            poi_stops_dir_stats=poi_stops_dir_stats,
        )

    write_errors_sheet(wb, city, bad)

    if removed_directions:
        write_removed_directions_sheet(wb, city, removed_directions)

    if unique_stops:
        write_unique_stops_sheet(wb, unique_stops, bbox, kml_routes=kml_routes)


    if heatmap:
        write_heatmap_sheet(wb, heatmap)

    # Добавлен вызов для листа дедупликации
    if dedup_removed:
        write_dedup_sheet(wb, dedup_removed)

    # Лист пассажиропотока пишется, когда расчёт состоялся. При неудаче лист
    # не создаётся вовсе, а причина уже напечатана в консоль: пустой лист с
    # нулями читался бы как «в городе никто не ездит».
    if passenger is not None and getattr(passenger, "ok", False):
        write_passenger_sheet(
            wb,
            passenger.result,
            network_report=passenger.network_report,
        )

    try:
        wb.save(path)
    except (PermissionError, OSError) as exc:
        logger.warning("Не удалось сохранить %s: %s", path, exc)
        return None

    return path


__all__ = ["build_xlsx_workbook"]