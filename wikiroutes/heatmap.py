import logging
import math

# Только генерация KML; парсинг недоверенного XML не выполняется.
import xml.etree.ElementTree as ET  # nosec B405
from pathlib import Path
from typing import Any

from .support import fix_stop_coord

logger = logging.getLogger("wikiroutes.heatmap")


def _lerp(a: float, b: float, t: float) -> int:
    return round(a + (b - a) * t)


def heat_color(t: float, alpha: str = "ff") -> str:
    stops = [
        (0, 0, 255),
        (0, 255, 255),
        (0, 255, 0),
        (255, 255, 0),
        (255, 0, 0),
    ]

    x = max(0.0, min(1.0, t)) * (len(stops) - 1)

    i = int(x)
    f = x - i

    if i >= len(stops) - 1:
        i = len(stops) - 2
        f = 1.0

    r = _lerp(stops[i][0], stops[i + 1][0], f)
    g = _lerp(stops[i][1], stops[i + 1][1], f)
    b = _lerp(stops[i][2], stops[i + 1][2], f)

    return f"{alpha}{b:02x}{g:02x}{r:02x}"


def build_heatmap(
    uniq_stops: dict[str, dict[str, Any]],
    bbox: tuple[float, float, float, float] | None,
    cell_km: float = 0.1,
    alpha: str = "ff",
    smooth: bool = False,
    gamma: float = 0.6,
    top_pct: float = 10.0,
) -> dict[str, Any] | None:
    if not bbox:
        return None

    min_lat, min_lon, max_lat, max_lon = bbox

    lat_c = (min_lat + max_lat) / 2

    dlat = cell_km / 111.0
    dlon = (
        cell_km / (111.0 * math.cos(math.radians(lat_c)))
        if abs(lat_c) < 89
        else cell_km / 111.0
    )

    stop_recs: list[tuple[float, float, float]] = []

    for rec in uniq_stops.values():
        fixed = fix_stop_coord(
            {"latitude": rec["lat"], "longitude": rec["lon"]},
            rec.get("idx", 0),
            bbox,
        )

        if not fixed:
            continue

        lat, lon = fixed

        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
            stop_recs.append((lat, lon, len(rec["routes"])))

    if not stop_recs:
        return None

    total_stops = len(stop_recs)

    if top_pct and 0 < top_pct < 100:
        stop_recs.sort(key=lambda item: -item[2])

        k = max(1, math.ceil(total_stops * top_pct / 100.0))
        stop_recs = stop_recs[:k]

    grid: dict[tuple[int, int], float] = {}

    for lat, lon, weight in stop_recs:
        grid_key = (
            int((lat - min_lat) / dlat),
            int((lon - min_lon) / dlon),
        )

        grid[grid_key] = grid.get(grid_key, 0.0) + weight

    if not grid:
        return None

    if smooth:
        blurred: dict[tuple[int, int], float] = {}

        for (i, j), value in grid.items():
            s = 0.5 * value

            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                s += 0.125 * grid.get((i + di, j + dj), 0.0)

            blurred[(i, j)] = s

        grid = blurred

    vmax = max(grid.values(), default=0.0)

    cells: list[dict[str, Any]] = []

    for (i, j), value in grid.items():
        lat0 = min_lat + i * dlat
        lon0 = min_lon + j * dlon

        cells.append(
            {
                "lat0": lat0,
                "lon0": lon0,
                "lat1": lat0 + dlat,
                "lon1": lon0 + dlon,
                "value": value,
                # vmax == 0 возможен, если все веса нулевые; не делим на ноль.
                "t": value / vmax if vmax > 0 else 0.0,
            }
        )

    cells.sort(key=lambda cell: -cell["value"])

    return {
        "cells": cells,
        "cell_km": cell_km,
        "max": vmax,
        "alpha": alpha,
        "smooth": smooth,
        "gamma": gamma,
        "top_pct": top_pct,
        "stops_total": total_stops,
        "stops_shown": len(stop_recs),
        "weight": "routes",
        "vol_radius": None,
    }


def build_heatmap_kml(
    heatmap: dict[str, Any] | None,
    city_title: str,
    path: Path | str,
    max_height: float = 400.0,
    flat: bool = False,
) -> Path | None:
    if not heatmap or not heatmap.get("cells"):
        return None

    kml = ET.Element("kml", xmlns="http://www.opengis.net/kml/2.2")
    doc = ET.SubElement(kml, "Document")

    ET.SubElement(doc, "name").text = f"{city_title} — тепловая карта остановок"

    metric_note = "число маршрутов"
    unit = "маршрутов"

    top = heatmap.get("top_pct") or 100
    top_note = f", топ-{top:g}% остановок" if 0 < top < 100 else ""

    ET.SubElement(doc, "description").text = (
        f"Значение ячейки = суммарный {metric_note} на остановках в ячейке.\n"
        f"Ячейка: {heatmap['cell_km']} км, максимум: {heatmap['max']:,.0f} {unit}\n"
        f"Остановок: {heatmap.get('stops_shown', '?')} из {heatmap.get('stops_total', '?')}"
    )

    levels = 32
    alpha = heatmap.get("alpha", "ff")
    gamma = heatmap.get("gamma", 1.0)

    for level in range(levels):
        style = ET.SubElement(doc, "Style", id=f"heat{level}")
        poly_style = ET.SubElement(style, "PolyStyle")

        ET.SubElement(poly_style, "color").text = heat_color(
            level / (levels - 1), alpha
        )
        ET.SubElement(poly_style, "outline").text = "0"

    folder = ET.SubElement(doc, "Folder")

    ET.SubElement(folder, "name").text = (
        f"Нагрузка остановок ({metric_note}, ячейка {heatmap['cell_km']} км{top_note}"
        f"{', сглаживание' if heatmap.get('smooth') else ''})"
    )

    for cell in heatmap["cells"]:
        t_adj = max(0.0, min(1.0, cell["t"])) ** gamma
        level = round(t_adj * (levels - 1))

        alt = 0.0 if flat else max_height * t_adj

        placemark = ET.SubElement(folder, "Placemark")

        ET.SubElement(placemark, "name").text = f"Значение: {cell['value']:,.0f} {unit}"
        ET.SubElement(placemark, "styleUrl").text = f"#heat{level}"

        polygon = ET.SubElement(placemark, "Polygon")

        ET.SubElement(polygon, "extrude").text = "0" if flat else "1"
        ET.SubElement(polygon, "altitudeMode").text = (
            "clampToGround" if flat else "relativeToGround"
        )
        ET.SubElement(polygon, "tessellate").text = "1"

        outer = ET.SubElement(polygon, "outerBoundaryIs")
        ring = ET.SubElement(outer, "LinearRing")

        ET.SubElement(ring, "coordinates").text = " ".join(
            f"{lon:.7f},{lat:.7f},{alt:.1f}"
            for lon, lat in [
                (cell["lon0"], cell["lat0"]),
                (cell["lon1"], cell["lat0"]),
                (cell["lon1"], cell["lat1"]),
                (cell["lon0"], cell["lat1"]),
                (cell["lon0"], cell["lat0"]),
            ]
        )

    if hasattr(ET, "indent"):
        ET.indent(kml, space="  ")

    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            + ET.tostring(kml, encoding="unicode"),
            encoding="utf-8",
        )
    except OSError:
        logger.exception("Тепловая карта KML не сохранена")
        return None
    else:
        return path
