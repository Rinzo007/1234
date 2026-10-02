"""Построитель листа «Сводка» XLSX-отчёта."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from ..models import RouteData
from ..support import collect_terminal_routes, source_label
from .helpers import cached_type_label, finish_sheet, safe_append


def summary_type_rows(ok: list[RouteData]) -> list[list[Any]]:
    """Строки сводки с количеством маршрутов по типам транспорта."""
    rows: list[list[Any]] = [
        [
            title,
            sum(1 for route in ok if cached_type_label(route.route_type) == label),
        ]
        for title, label in (
            ("Троллейбусов", "Троллейбус"),
            ("Трамваев", "Трамвай"),
            ("Автобусов", "Автобус"),
            ("Маршруток", "Маршрутное такси"),
            ("Водных маршрутов", "Водный транспорт"),
            ("Монорельсов", "Монорельс"),
        )
    ]
    trains = sum(1 for route in ok if cached_type_label(route.route_type) == "Поезд")
    return [["Поездов", trains], *rows]


def summary_source_rows(ok: list[RouteData]) -> list[list[Any]]:
    """Строки сводки с количеством успешных маршрутов по источникам данных."""
    counts: dict[str, int] = {}
    for route in ok:
        label = source_label(route.source)
        counts[label] = counts.get(label, 0) + 1
    return [[f"Источник: {label}", counts[label]] for label in sorted(counts)]


def summary_rows(
    *,
    city: str,
    city_title: str,
    routes: Sequence[RouteData],
    ok: list[RouteData],
    bad: list[RouteData],
    ideas: list[RouteData],
    skipped_inactive: int,
    curv_limit: float,
    minlen_limit: float,
    radius_limit: float,
    maxlen_limit: float = 0.0,
    cut_curv: int,
    cut_len: int,
    cut_radius: int,
    removed_direction_routes: int = 0,
    fully_excluded_routes: int = 0,
    dedup_removed: Sequence[Mapping[str, Any]] | None,
    dedup_analysis: Mapping[str, Any] | None,
    net_metrics: Mapping[str, Any] | None,
    net_density: Any,
    kml_routes: Sequence[RouteData] | None,
    unique_stops: Mapping[str, Mapping[str, Any]] | None,
    poi_buffer_m: float,
    poi_stats: Mapping[Any, Any] | None,
    poi_stops_stats: Mapping[Any, Any] | None = None,
    overture_stats: Mapping[Any, Any] | None,
    overture_meta: Mapping[str, Any] | None,
) -> list[list[Any]]:
    """Все строки листа «Сводка»."""
    rows: list[list[Any]] = [
        ["Город", city_title],
        ["Slug", city],
        ["Дата", datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")],
        ["Маршрутов в обработке", len(routes)],
        ["Успешно (в отчёте)", len(ok)],
        ["Ошибок", len(bad)],
        ["Идей пассажиров", len(ideas)],
        ["Пропущено неактивных (--active-only)", skipped_inactive],
        ["Отсечено направлений: криволинейность", cut_curv],
        ["Отсечено направлений: длина", cut_len],
        ["Отсечено направлений: радиус", cut_radius],
        ["Маршрутов с удалёнными направлениями", removed_direction_routes],
        ["Маршрутов исключено целиком", fully_excluded_routes],
        ["Дедупликация: удалено направлений", len(dedup_removed) if dedup_removed else 0],
        ["Дедупликация: Км до", dedup_analysis.get("km_coef", "") if dedup_analysis else ""],
        ["Маршрутный коэффициент Км", net_metrics.get("km_coef", "") if net_metrics else ""],
        ["Суммарная длина трасс, км", net_metrics.get("total_km", "") if net_metrics else ""],
        ["Длина уникальной сети, км", net_metrics.get("unique_km", "") if net_metrics else ""],
        ["Плотность сети, км/км²", net_density],
        ["Макс. коэфф. непрямолинейности (0=без лимита)", curv_limit],
        ["Мин. длина маршрута, км (0=без лимита)", minlen_limit],
        ["Макс. длина маршрута, км (0=без лимита)", maxlen_limit],
        ["Радиус от центра, км (0=без лимита)", radius_limit],
        ["Маршрутов на карте (KML)", len(kml_routes) if kml_routes is not None else 0],
        ["Остановок уникальных", len(unique_stops) if unique_stops is not None else 0],
        ["Конечных", len(collect_terminal_routes(ok))],
    ]

    rows.extend(summary_type_rows(ok))
    rows.extend(summary_source_rows(ok))
    rows.extend(
        [
            ["POI: буфер, м", poi_buffer_m if poi_stats else ""],
            # Строка "POI: суммарное value" удалена
        ]
    )

    if poi_stops_stats:
        total_pois = sum(stat.count for stat in poi_stops_stats.values())
        rows.append(["POI-stops: суммарное число POI", total_pois])
        # Строка "POI-stops: маршрутов" удалена

    if overture_meta:
        rows.append(["Overture: буфер, м", overture_meta.get("buffer_m", "")])

    if overture_stats:
        total_area = sum(stat.total_area_m2 for stat in overture_stats.values())
        rows.append(["Overture: суммарная площадь, тыс. м²", round(total_area / 1e3, 1)])

    return rows


def write_summary_sheet(wb: Any, rows: Sequence[Sequence[Any]]) -> None:
    """Записывает лист «Сводка»."""
    ws = wb.active if wb.active is not None else wb.create_sheet()
    ws.title = "Сводка"
    safe_append(ws, ["Параметр", "Значение"])
    for row in rows:
        safe_append(ws, list(row))
    finish_sheet(ws, ["Параметр", "Значение"], wrap={2}, fixed_widths=[25, 35])


__all__ = ["summary_rows", "summary_source_rows", "summary_type_rows", "write_summary_sheet"]