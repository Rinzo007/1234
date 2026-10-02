"""Экспорт маршрутной сети, остановок и аналитики в KML 2.2."""

from __future__ import annotations

import logging
import math

# Только генерация KML; парсинг недоверенного XML не выполняется.
import xml.etree.ElementTree as ET  # nosec B405
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .compat import stop_name
from .constants import BASE_URL
from .geometry import polyline_km
from .support import (
    fix_stop_coord,
    fmt_curv_str,
    format_dedup_id,
    source_label,
    type_label,
)

logger = logging.getLogger("wikiroutes.kml")

KML_LINE_COLOR = {
    "trolleybus": "ffff0000",
    "tram": "ff0000ff",
    "water": "ff808000",
    "bus": "ff00aa00",
    "metro": "ff800080",
    "train": "ff003264",
    "funicular": "ffcc66ff",
    "cable": "ffffff00",
    "monorail": "ff00ffff",
    "electrobus": "ff008cff",
    "idea": "ffff00ff",
}
KML_STOP_COLOR = {
    "trolleybus": "ccff0000",
    "tram": "cc0000ff",
    "water": "ff408080",
    "bus": "ff008800",
    "metro": "cc800080",
    "train": "cc003264",
    "funicular": "cccc66ff",
    "cable": "ccffff00",
    "monorail": "cc00ffff",
    "electrobus": "cc008cff",
    "idea": "ccff00ff",
}
KML_STOP_ICON = {
    "trolleybus": "https://maps.google.com/mapfiles/kml/shapes/bus.png",
    "tram": "https://maps.google.com/mapfiles/kml/shapes/rail.png",
    "water": "https://maps.google.com/mapfiles/kml/shapes/ferry.png",
    "bus": "https://maps.google.com/mapfiles/kml/shapes/bus.png",
    "metro": "https://maps.google.com/mapfiles/kml/shapes/rail.png",
    "train": "https://maps.google.com/mapfiles/kml/shapes/rail.png",
    "funicular": "https://maps.google.com/mapfiles/kml/shapes/rail.png",
    "cable": "https://maps.google.com/mapfiles/kml/shapes/placemark_circle.png",
    "monorail": "https://maps.google.com/mapfiles/kml/shapes/rail.png",
    "electrobus": "https://maps.google.com/mapfiles/kml/shapes/bus.png",
    "idea": "https://maps.google.com/mapfiles/kml/shapes/info-i.png",
}
KML_FOLDER_LABELS = {
    "trolleybus": "🚎 Троллейбусы",
    "tram": "🚊 Трамваи",
    "water": "⛴ Водный транспорт",
    "bus": "🚌 Автобусы",
    "metro": "🚇 Метро",
    "train": "🚆 Поезда",
    "funicular": "🚞 Фуникулёры",
    "cable": "🚠 Канатные дороги",
    "monorail": "🚝 Монорельсы",
    "electrobus": "🔌 Электробусы",
    "idea": "💡 Идеи пассажиров",
}


def _valid_coord(lat: Any, lon: Any) -> tuple[float, float] | None:
    """Возвращает валидную пару WGS84 или ``None``."""
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat_f) and math.isfinite(lon_f)):
        return None
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return None
    return lat_f, lon_f


def _valid_coords(coords: Any) -> list[tuple[float, float]]:
    """Валидные WGS84-точки последовательности координат."""
    return [
        point
        for point in (_valid_coord(*coord) for coord in (coords or ()))
        if point is not None
    ]


def _route_type_key(route: Any) -> Any:
    return getattr(route.route_type, "value", route.route_type)


def _route_sort_key(route: Any) -> tuple[Any, Any]:
    return (_route_type_key(route), route.name)


def _add_line_style(doc: Any, sid: str, color: str, width: str) -> None:
    st = ET.SubElement(doc, "Style", id=sid)
    ls = ET.SubElement(st, "LineStyle")
    ET.SubElement(ls, "color").text = color
    ET.SubElement(ls, "width").text = width


def _add_icon_style(doc: Any, sid: str, color: str, scale: str, href: str) -> None:
    st = ET.SubElement(doc, "Style", id=sid)
    ic = ET.SubElement(st, "IconStyle")
    ET.SubElement(ic, "color").text = color
    ET.SubElement(ic, "scale").text = scale
    ET.SubElement(ET.SubElement(ic, "Icon"), "href").text = href


def _stat_for(
    dir_stats: Mapping[Any, Any] | None,
    route_stats: Mapping[Any, Any] | None,
    route_id: int,
    di: int,
) -> Any:
    """Статистика направления, затем маршрута; иначе ``None``."""
    if dir_stats is not None and (route_id, di) in dir_stats:
        return dir_stats[(route_id, di)]
    if route_stats is not None and route_id in route_stats:
        return route_stats[route_id]
    return None


def _append_stat_line(
    lines: list[str],
    st: Any,
    buffer_m: float,
    render: Callable[[Any, float], str],
) -> None:
    if not st:
        return
    lines.append(render(st, buffer_m))


def _add_stop_placemark(folder: Any, rt_key: Any, s: Any, si: int, bbox: Any) -> None:
    c = fix_stop_coord(s, si, bbox)
    if not c or _valid_coord(c[0], c[1]) is None:
        return
    sp = ET.SubElement(folder, "Placemark")
    ET.SubElement(sp, "name").text = stop_name(s) or "?"
    ET.SubElement(sp, "styleUrl").text = f"#stop_{rt_key}"
    ET.SubElement(ET.SubElement(sp, "Point"), "coordinates").text = (
        f"{c[1]:.7f},{c[0]:.7f},0"
    )


def _add_direction_placemark(
    folder: Any,
    rd: Any,
    di: int,
    d: Any,
    *,
    dir_orig: Mapping[Any, int] | None,
    include_stops: bool,
    bbox: tuple[float, float, float, float] | None,
    show_source: bool,
    poi_stats: Mapping[Any, Any] | None,
    poi_buffer: float,
    poi_dir_stats: Mapping[Any, Any] | None,
    overture_stats: Mapping[Any, Any] | None,
    overture_meta: Mapping[str, Any] | None,
    overture_dir_stats: Mapping[Any, Any] | None,
) -> None:
    """Рисует один маршрут (направление) и при необходимости его остановки."""
    if not d.coords:
        return
    valid = _valid_coords(d.coords)
    if len(valid) < 2:
        return

    rt_key = _route_type_key(rd)
    pm = ET.SubElement(folder, "Placemark")
    name_suffix = " (Идея)" if rd.is_idea else ""
    # Исходный di (до вырезаний дедупа/фильтров): позиционный di
    # после apply_dedup_removals смещён и крадёт ключ удалённого
    # направления. Статистика ниже — по текущим позициям.
    orig_di = dir_orig.get((rd.route_id, di), di) if dir_orig else di
    dir_word = "туда" if orig_di == 0 else "обратно"
    dir_id = format_dedup_id((rd.route_id, orig_di))
    ET.SubElement(
        pm, "name"
    ).text = f"{rd.name}{name_suffix} ({dir_word}) [{dir_id}]"

    lines = [
        f"Маршрут: {rd.name}",
        f"ID направления: {dir_id}",
        f"Тип: {type_label(rd.route_type)}",
    ]
    if show_source:
        lines.append(
            f"Источник: {source_label(rd.source)}"
        )
    lines += [
        f"Активен: {'Да' if rd.active else 'Нет'}",
        f"Направление: {d.name or '—'}",
        f"Длина: {d.km:.1f} км",
        f"Коэфф. непрямолинейности: {fmt_curv_str(d.curvilinearity)}",
        f"Остановок: {len(d.stops)}",
        f"Ссылка: {rd.url}",
    ]
    if rd.is_idea:
        lines.append(f"Автор: {rd.company}")
        rating_str = rd.transport_class
        if rating_str and rating_str != "()":
            lines.append(f"Рейтинг: {rating_str}")

    ov = _stat_for(overture_dir_stats, overture_stats, rd.route_id, di)
    if ov is not None and getattr(ov, "total_area_m2", 0) > 0:
        buf_o = overture_meta.get("buffer_m", 0) if overture_meta else 0
        lines.append(
            f"Площадь объектов Overture (буфер {buf_o:.0f} м): "
            f"{ov.total_area_m2 / 1e3:.1f} тыс. м²"
        )
    _append_stat_line(
        lines,
        _stat_for(poi_dir_stats, poi_stats, rd.route_id, di),
        poi_buffer,
        lambda st, buf: f"POI в буфере {buf:.0f} м: "
        f"{st.count} точек, value={st.total_value:.1f}",
    )

    ET.SubElement(pm, "description").text = "\n".join(lines)
    ET.SubElement(pm, "styleUrl").text = f"#{rt_key}"
    ls = ET.SubElement(pm, "LineString")
    ET.SubElement(ls, "tessellate").text = "1"
    ET.SubElement(ls, "coordinates").text = "\n".join(
        f"{lon:.7f},{lat:.7f},0" for lat, lon in valid
    )

    if include_stops:
        for si, s in enumerate(d.stops):
            _add_stop_placemark(folder, rt_key, s, si, bbox)


def _serialize_kml(kml: ET.Element, path: str | Path) -> str | Path | None:
    """Индентирует и сохраняет KML; при ошибке записи возвращает ``None``."""
    if hasattr(ET, "indent"):
        ET.indent(kml, space="  ")

    try:
        with Path(path).open("w", encoding="utf-8") as out_f:
            out_f.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                + ET.tostring(kml, encoding="unicode")
            )
    except OSError as e:
        logger.warning("KML: %s", e)
        return None
    return path


def build_kml(
    routes: Sequence[Any],
    city_title: str,
    path: str | Path,
    include_stops: bool = False,
    bbox: tuple[float, float, float, float] | None = None,
    dir_orig: Mapping[Any, int] | None = None,
    poi_stats: Mapping[Any, Any] | None = None,
    poi_buffer: float = 0.0,
    poi_dir_stats: Mapping[Any, Any] | None = None,
    overture_stats: Mapping[Any, Any] | None = None,
    overture_meta: Mapping[str, Any] | None = None,
    overture_dir_stats: Mapping[Any, Any] | None = None,
) -> str | Path | None:
    """Создаёт KML 2.2 и сохраняет его по указанному пути.

    Метрики POI и Overture при наличии записываются
    на уровень отдельного направления маршрута.
    """
    kml = ET.Element("kml", xmlns="http://www.opengis.net/kml/2.2")
    doc = ET.SubElement(kml, "Document")
    ET.SubElement(doc, "name").text = f"{city_title} — маршрутная сеть"
    ET.SubElement(doc, "description").text = (
        f"Источник: {BASE_URL}\n"
        f"Город: {city_title}\n"
        f"Маршрутов: {len(routes)}\n"
        f"Дата: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
    )

    present_types = {
        _route_type_key(r)
        for r in routes
        if not r.error and r.directions
    }
    show_source = (
        len({r.source for r in routes if not r.error and r.directions}) > 1
    )
    for rt in present_types:
        _add_line_style(doc, rt, KML_LINE_COLOR.get(rt, KML_LINE_COLOR["bus"]), "3")
    for rt in present_types:
        _add_icon_style(
            doc,
            f"stop_{rt}",
            KML_STOP_COLOR.get(rt, KML_STOP_COLOR["bus"]),
            "0.6",
            KML_STOP_ICON.get(
                rt,
                "https://maps.google.com/mapfiles/kml/shapes/bus.png",
            ),
        )

    folders = {}
    for rt in sorted(present_types):
        folder = ET.SubElement(doc, "Folder")
        ET.SubElement(folder, "name").text = KML_FOLDER_LABELS.get(rt, type_label(rt))
        folders[rt] = folder

    for rd in sorted(routes, key=_route_sort_key):
        if rd.error or not rd.directions:
            continue
        rt_key = _route_type_key(rd)
        folder = folders.get(rt_key)
        if folder is None:
            continue

        for di, d in enumerate(rd.directions):
            _add_direction_placemark(
                folder,
                rd,
                di,
                d,
                dir_orig=dir_orig,
                include_stops=include_stops,
                bbox=bbox,
                show_source=show_source,
                poi_stats=poi_stats,
                poi_buffer=poi_buffer,
                poi_dir_stats=poi_dir_stats,
                overture_stats=overture_stats,
                overture_meta=overture_meta,
                overture_dir_stats=overture_dir_stats,
            )

    return _serialize_kml(kml, path)


def _dir_label(rk: tuple) -> str:
    return (
        "туда"
        if (isinstance(rk, tuple) and len(rk) == 2 and rk[1] == 0)
        else "обратно"
    )


def _placemark_name(route_name: str, rk: tuple) -> str:
    """Имя метки: название + туда/обратно + id направления (route_id/di)."""
    return f"{route_name} ({_dir_label(rk)}) [{format_dedup_id(rk)}]"


def _orig_key(rk: tuple, dir_orig: Mapping[Any, int] | None) -> tuple:
    """Исходный ключ направления до вырезаний (позиционный — после).

    Записи дедупликации ссылаются на исходные (route_id, di), а
    ``routes`` после apply_dedup_removals пересобраны: выживший объект
    со старым di=N оказывается под позицией di<M. Без отображения
    выживший крадёт ключ жертвы — и карта противоречит листу удалений
    («заменитель говорит, что заменил X, а X среди выживших»).
    """
    if dir_orig and isinstance(rk, tuple) and len(rk) == 2:
        return (rk[0], dir_orig.get(rk, rk[1]))
    return rk


def _route_dirs(routes: Sequence[Any]) -> dict[tuple, tuple[Any, Any]]:
    """Индекс направлений (route_id, di) → (маршрут, направление)."""
    collected: dict[tuple, tuple[Any, Any]] = {}
    for rd in routes:
        if getattr(rd, "error", False) or not getattr(rd, "directions", ()):
            continue
        for di, d in enumerate(rd.directions):
            collected[(rd.route_id, di)] = (rd, d)
    return collected


def _entry_geometry(entry: Mapping[str, Any], field: str) -> dict[str, Any] | None:
    info = entry.get(field)
    if isinstance(info, dict) and info.get("координаты"):
        return info
    return None


def _survivor_by_orig(
    survivors: Mapping[tuple, tuple[Any, Any]],
    dir_orig: Mapping[Any, int] | None,
    rep: tuple,
) -> tuple[Any, Any] | None:
    """Геометрия выжившего по исходному ключу (для заменителя без координат).

    Прямое совпадение — когда индексы не смещались; иначе ищем объект
    среди выживших через отображение исходных di.
    """
    direct = survivors.get(rep)
    if direct is not None:
        return direct
    if not dir_orig or not (isinstance(rep, tuple) and len(rep) == 2):
        return None
    for (srid, sdi), val in survivors.items():
        if srid == rep[0] and dir_orig.get((srid, sdi), sdi) == rep[1]:
            return val
    return None


class _DedupKmlState:
    """Накапливаемое состояние разбора записей дедупликации."""

    __slots__ = (
        "all_dirs",
        "dir_orig",
        "label_by_key",
        "map_type",
        "rep_graph",
        "replaced_items",
        "replacer_done",
        "replacer_items",
        "replacer_replaced",
        "survive_keys",
        "survivors",
    )

    def __init__(
        self,
        survivors: dict[tuple, tuple[Any, Any]],
        all_dirs: dict[tuple, tuple[Any, Any]],
        dir_orig: Mapping[Any, int] | None,
        map_type: str,
        survive_keys: set[tuple],
    ) -> None:
        self.survivors = survivors
        self.all_dirs = all_dirs
        self.dir_orig = dir_orig
        self.map_type = map_type
        self.survive_keys = survive_keys
        self.replaced_items: list[tuple] = []
        self.replacer_items: list[tuple] = []
        self.replacer_replaced: dict[tuple, list[str]] = {}
        # Граф прямых замен (представитель → заменённые направления) и метки
        # направлений — для транзитивного замыкания цепочек (жертва может и сама
        # быть представителем для более коротких дублей: 4 → 5 → 5т).
        self.rep_graph: dict[tuple, list[tuple]] = {}
        self.label_by_key: dict[tuple, str] = {}
        self.replacer_done: set[tuple] = set()


def _ingest_dedup_entry(entry: Mapping[str, Any], state: _DedupKmlState) -> None:
    """Принимает одну запись дедупликации в коллекции слоёв карты."""
    marked = entry.get("маршрут")
    if not (isinstance(marked, tuple) and len(marked) == 2):
        return

    info = _entry_geometry(entry, "маршрут_координаты")
    dir_name: Any = None
    if info is not None:
        coords_list = _valid_coords(info.get("координаты"))
        rtype = info.get("тип")
        route_name = info.get("маршрут") or format_dedup_id(marked)
        dir_name = info.get("название")
    else:
        rr = state.all_dirs.get(marked)
        if rr is None:
            return
        rd, d = rr
        rtype = _route_type_key(rd)
        route_name = rd.name
        dir_name = d.name
        coords_list = _valid_coords(d.coords)

    if rtype != state.map_type:
        return
    if len(coords_list) >= 2:
        state.replaced_items.append((marked, rtype, route_name, dir_name, coords_list))

    rep = entry.get("представитель")
    if not (isinstance(rep, tuple) and len(rep) == 2):
        return
    rep_label = f"{route_name} ({_dir_label(marked)}) [{format_dedup_id(marked)}]"
    if rep_label not in state.replacer_replaced.setdefault(rep, []):
        state.replacer_replaced[rep].append(rep_label)
    state.label_by_key[marked] = rep_label
    if marked not in state.rep_graph.setdefault(rep, []):
        state.rep_graph[rep].append(marked)
    if rep in state.survive_keys or rep in state.replacer_done:
        # Заменитель выжил сам (или линия уже построена): отдельную
        # линию не рисуем, чтобы не дублировать геометрию выжившего.
        # Сведения «кого заменил» уже перенесены выше и попадут
        # в описание выжившего.
        return

    rep_info = _entry_geometry(entry, "представитель_координаты")
    if rep_info is not None:
        rep_coords_list = _valid_coords(rep_info.get("координаты"))
        rep_rtype = rep_info.get("тип")
        rep_name = rep_info.get("маршрут") or format_dedup_id(rep)
        rep_dir_name = rep_info.get("название")
    else:
        sr = _survivor_by_orig(state.survivors, state.dir_orig, rep)
        if sr is None:
            return
        rd2, d2 = sr
        rep_rtype = _route_type_key(rd2)
        rep_name = rd2.name
        rep_dir_name = d2.name
        rep_coords_list = _valid_coords(d2.coords)

    if len(rep_coords_list) >= 2:
        state.replacer_items.append(
            (rep, rep_rtype, rep_name, rep_dir_name, rep_coords_list)
        )
        state.replacer_done.add(rep)


def _transitive_replaced(
    rep_graph: dict[tuple, list[tuple]],
    label_by_key: dict[tuple, str],
    rep: tuple[int, int],
) -> list[str]:
    """Транзитивное замыкание цепочек замен.

    Если представитель сам был удалён как дубль (4 → 5 → 5т), то
    заменённые им направления также заменил его победитель — переносим
    их в его список «Заменил».
    """
    result: list[str] = []
    seen_labels: set[str] = set()
    visited: set[tuple[int, int]] = set()
    queue = list(rep_graph.get(rep, ()))
    head = 0
    while head < len(queue):
        key = queue[head]
        head += 1
        if key in visited:
            continue
        visited.add(key)
        label = label_by_key.get(key)
        if label is not None and label not in seen_labels:
            seen_labels.add(label)
            result.append(label)
        queue.extend(rep_graph.get(key, ()))
    return result


def _add_line_placemark(
    folder: Any,
    style: str,
    name: str,
    coords: Sequence[Any],
    lines: Sequence[str],
) -> None:
    pm = ET.SubElement(folder, "Placemark")
    ET.SubElement(pm, "name").text = name
    ET.SubElement(pm, "styleUrl").text = style
    ET.SubElement(pm, "description").text = "\n".join(lines)
    ls = ET.SubElement(pm, "LineString")
    ET.SubElement(ls, "tessellate").text = "1"
    ET.SubElement(ls, "coordinates").text = "\n".join(
        f"{lon:.7f},{lat:.7f},0" for lat, lon in coords
    )


def _prebuild_replacer_styles(
    doc: Any, replacer_items: Sequence[Any]
) -> dict[str, str]:
    """Стили заменителей (по собственному типу маршрута)."""
    styles: dict[str, str] = {}
    for rk, rtype, _name, _dir, _coords in replacer_items:
        key = rtype or "other"
        sid = f"mt_replacer_{key}"
        if key not in styles:
            _add_line_style(doc, sid, KML_LINE_COLOR.get(rtype, "ff00ff00"), "3")
            styles[key] = sid
    return styles


def _draw_survive_placemarks(
    folders: dict,
    style_map: Mapping[str, str],
    survive_items: Sequence[Any],
    replacer_replaced: Mapping[tuple, Sequence[str]],
) -> None:
    for rk, rd, d in survive_items:
        coords = _valid_coords(d.coords)
        if len(coords) < 2:
            continue
        lines = [
            f"Маршрут: {rd.name}",
            f"ID направления: {format_dedup_id(rk)}",
            f"Тип: {type_label(rd.route_type)}",
            f"Направление: {d.name or '—'}",
            f"Длина: {d.km:.1f} км",
        ]
        replaced_labels = replacer_replaced.get(rk)
        if replaced_labels:
            lines.append("Заменил: " + ", ".join(replaced_labels))
        _add_line_placemark(
            folders["survive"][1],
            style_map["survive"],
            _placemark_name(rd.name, rk),
            coords,
            lines,
        )


def _draw_replaced_placemarks(
    folders: dict,
    style_map: Mapping[str, str],
    replaced_items: Sequence[Any],
) -> None:
    for rk, rtype, route_name, dir_name, coords in replaced_items:
        if len(coords) < 2:
            continue
        _add_line_placemark(
            folders["replaced"][1],
            style_map["replaced"],
            _placemark_name(route_name, rk),
            coords,
            [
                f"Маршрут: {route_name}",
                f"ID направления: {format_dedup_id(rk)}",
                f"Тип: {type_label(rtype)}",
                f"Направление: {dir_name or '—'}",
                f"Длина: {polyline_km(coords):.1f} км",
            ],
        )


def _draw_replacer_placemarks(
    folders: dict,
    replacer_styles: Mapping[str, str],
    replacer_items: Sequence[Any],
    replacer_replaced: Mapping[tuple, Sequence[str]],
) -> None:
    for rk, rtype, route_name, dir_name, coords in replacer_items:
        if len(coords) < 2:
            continue
        lines = [
            f"Маршрут: {route_name}",
            f"ID направления: {format_dedup_id(rk)}",
            f"Тип: {type_label(rtype)}",
            f"Направление: {dir_name or '—'}",
            f"Длина: {polyline_km(coords):.1f} км",
        ]
        replaced_labels = replacer_replaced.get(rk)
        if replaced_labels:
            lines.append("Заменил: " + ", ".join(replaced_labels))
        _add_line_placemark(
            folders["replacer"][1],
            "#" + replacer_styles[rtype or "other"],
            _placemark_name(route_name, rk),
            coords,
            lines,
        )


def build_map_dedup_kml(
    routes: Sequence[Any],
    all_routes: Sequence[Any],
    dedup_removed: Sequence[Mapping[str, Any]] | None,
    city_title: str,
    path: str | Path,
    map_type: str,
    dir_orig: Mapping[Any, int] | None = None,
) -> str | Path | None:
    """Строит KML с выбранным типом направления и его заменителями.

    Рисуются три слоя:
      * выжившие направления выбранного типа (``routes`` — после dedup);
      * заменённые направления этого типа — дубликаты, удалённые при
        дедупликации (восстанавливаются из ``all_routes`` по ключу
        ``(route_id, di)`` записи ``dedup_removed``);
      * заменившие направления — «представители», которые заменили
        удалённые направления этого типа (из ``dedup_removed["представитель"]``).
    """
    kml = ET.Element("kml", xmlns="http://www.opengis.net/kml/2.2")
    doc = ET.SubElement(kml, "Document")
    ET.SubElement(doc, "name").text = f"{city_title} — {type_label(map_type)}"
    ET.SubElement(doc, "description").text = (
        f"Город: {city_title}\nТип: {type_label(map_type)}\n"
        f"Дата: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
    )

    _add_line_style(
        doc,
        "mt_survive",
        KML_LINE_COLOR.get(map_type, KML_LINE_COLOR["bus"]),
        "3",
    )
    _add_line_style(doc, "mt_replaced", "ff0000ff", "3")  # удалённые дубликаты
    # Заменители окрашиваются по собственному типу (как оригинал того же типа):
    # например, трамвай-заменитель — красный, как и выживший трамвай.

    survivors = _route_dirs(routes)
    all_dirs = _route_dirs(all_routes)

    # Слой 1: выжившие направления выбранного типа (ключи — исходные,
    # через _orig_key: иначе после переиндексации сторож заменителей
    # rep in survive_keys не срабатывает и та же линия рисуется дважды).
    survive_items = [
        (_orig_key(rk, dir_orig), rd, d)
        for rk, (rd, d) in survivors.items()
        if _route_type_key(rd) == map_type
    ]
    survive_keys = {rk for rk, _rd, _d in survive_items}

    # Слой 2: заменённые (удалённые) направления этого типа + их заменители.
    # Геометрия берётся из записей dedup (маршрут_координаты), а при их
    # отсутствии — из all_routes по ключу (индекс может быть смещён после
    # фильтрации, поэтому встроенные координаты надёжнее).
    state = _DedupKmlState(survivors, all_dirs, dir_orig, map_type, survive_keys)
    for entry in dedup_removed or []:
        _ingest_dedup_entry(entry, state)

    # Звенья цепочек: представитель, который сам стал жертвой (4 → 5 → 5т —
    # звено 5). Его отдельную линию не рисуем: геометрия уже покрыта
    # финальным победителем цепочки, а сведения «кого заменил» перенесены
    # в описание финального представителя (см. _transitive_replaced).
    chain_keys = {
        key for kids in state.rep_graph.values() for key in kids if key in state.rep_graph
    }
    if chain_keys:
        state.replacer_items = [
            item for item in state.replacer_items if item[0] not in chain_keys
        ]

    # Транзитивное замыкание цепочек замен (4 → 5 → 5т): победитель
    # получает и замены промежуточных звеньев.
    for rep in list(state.replacer_replaced):
        state.replacer_replaced[rep] = _transitive_replaced(
            state.rep_graph, state.label_by_key, rep
        )

    folders = {
        "survive": ("Выжившие", ET.SubElement(doc, "Folder")),
        "replaced": ("Заменённые (дубликаты)", ET.SubElement(doc, "Folder")),
        "replacer": ("Заменившие (представители)", ET.SubElement(doc, "Folder")),
    }
    for label, folder in folders.values():
        ET.SubElement(folder, "name").text = f"{label} {type_label(map_type)}"

    style_map = {
        "survive": "#mt_survive",
        "replaced": "#mt_replaced",
    }
    replacer_styles = _prebuild_replacer_styles(doc, state.replacer_items)

    _draw_survive_placemarks(folders, style_map, survive_items, state.replacer_replaced)
    _draw_replaced_placemarks(folders, style_map, state.replaced_items)
    _draw_replacer_placemarks(
        folders, replacer_styles, state.replacer_items, state.replacer_replaced
    )

    return _serialize_kml(kml, path)