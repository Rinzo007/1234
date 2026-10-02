"""Простые листы XLSX без предметной аналитической логики."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..support import fix_stop_coord, source_label
from .helpers import cached_type_label, finish_sheet, safe_append


def write_errors_sheet(wb: Any, city: str, bad: Sequence[Any]) -> None:
    ws = wb.create_sheet("Ошибки")
    headers = ["Город", "Тип", "Номер", "ID", "Источник", "Ошибка", "Ссылка"]
    safe_append(ws, headers)
    for route in bad:
        safe_append(ws, [
            city, cached_type_label(route.route_type), route.name,
            route.route_id, source_label(route.source), route.error, route.url,
        ])
    finish_sheet(
        ws, headers, wrap={6}, link_col=7,
        fixed_widths=[12, 10, 12, 8, 12, 40, 30],
    )


def write_removed_directions_sheet(
    wb: Any,
    city: str,
    removed: Sequence[tuple[Any, Any, str]],
) -> None:
    """Лист с направлениями, отброшенными фильтрами по отдельности.

    ``removed`` — последовательность кортежей ``(route, direction, reason)``,
    где ``route`` и ``direction`` — доменные модели, ``reason`` — причина
    исключения направления.
    """
    ws = wb.create_sheet("Отсечённые направления")
    headers = [
        "Город", "Тип", "Номер", "ID", "Источник", "№ напр.", "Длина, км",
        "Причина", "Ссылка",
    ]
    safe_append(ws, headers)
    for route, direction, reason in removed:
        try:
            index = route.directions.index(direction) + 1
        except ValueError:
            index = ""
        length_km = direction.km if direction.km else None
        safe_append(ws, [
            city,
            cached_type_label(route.route_type),
            route.name,
            route.route_id,
            source_label(route.source),
            index,
            round(length_km, 3) if length_km is not None else None,
            reason,
            route.url,
        ])
    finish_sheet(
        ws,
        headers,
        num_fmt={7: "0.000"},
        wrap={8},
        link_col=9,
        fixed_widths=[12, 10, 12, 8, 12, 8, 12, 20, 30],
    )


def _stop_coord(rec: Mapping[str, Any], bbox: Any) -> Any:
    if rec.get("lat") is None or rec.get("lon") is None:
        return None
    return fix_stop_coord(
        {"latitude": rec["lat"], "longitude": rec["lon"]},
        rec.get("idx", 0),
        bbox,
    )


def _route_labels(kml_routes: Sequence[Any] | None) -> dict[int, str]:
    """Отображает id маршрута в строку «Тип Номер» по списку маршрутов."""
    if not kml_routes:
        return {}
    return {
        route.route_id: f"{cached_type_label(route.route_type)} {route.name}"
        for route in kml_routes
    }


def write_unique_stops_sheet(
    wb: Any,
    unique_stops: Mapping[str, Mapping[str, Any]],
    bbox: Any,
    kml_routes: Sequence[Any] | None = None,
) -> None:
    ws = wb.create_sheet("Остановки")
    headers = ["Название", "ID", "Типы", "Маршрутов", "Маршруты", "Широта", "Долгота"]
    labels = _route_labels(kml_routes)
    safe_append(ws, headers)
    for rec in sorted(unique_stops.values(), key=lambda r: (str(r["name"]).lower(), str(r["id"]))):
        coord = _stop_coord(rec, bbox)
        routes_list = sorted(
            (labels.get(rid, str(rid)) for rid in sorted(rec["routes"])),
            key=lambda value: value.lower(),
        )
        safe_append(ws, [
            rec["name"], rec["id"] if rec["id"] is not None else "",
            ", ".join(cached_type_label(t) for t in sorted(rec["types"])),
            len(rec["routes"]), ", ".join(routes_list),
            coord[0] if coord else None, coord[1] if coord else None,
        ])
    finish_sheet(
        ws,
        headers,
        num_fmt={6: "0.0000000", 7: "0.0000000"},
        wrap={1, 5},
        fixed_widths=[25, 12, 20, 10, 45, 15, 15],
    )


def write_heatmap_sheet(wb: Any, heatmap: Mapping[str, Any]) -> None:
    ws = wb.create_sheet("Тепловая карта")
    headers = ["№", "Широта центра", "Долгота центра", "Значение", "Доля от макс, %"]
    safe_append(ws, headers)
    for index, cell in enumerate(heatmap["cells"], 1):
        safe_append(ws, [
            index, round((cell["lat0"] + cell["lat1"]) / 2, 6),
            round((cell["lon0"] + cell["lon1"]) / 2, 6),
            round(cell["value"], 1), round(cell["t"] * 100, 1),
        ])
    finish_sheet(ws, headers, num_fmt={2: "0.000000", 3: "0.000000", 5: "0.0"}, fixed_widths=[6, 15, 15, 12, 12])
