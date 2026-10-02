"""Лист «Пассажиропоток» XLSX-отчёта: подробный разбор расчёта.

Отчёт нужен не для красоты, а чтобы ответить на вопрос «почему показатель такой».
Число без разбора — это утверждение; разбор — это проверка. Поэтому лист пишет
не только итог, но и **происхождение данных**, из которых итог получен.

Особенно важна строка о происхождении интервалов. Интервалы в каталоге
перевозчиков практически отсутствуют, и большинство значений в расчёте —
допущение по режиму. Если это не видно в отчёте, число выглядит измеренным,
будучи построенным на предположении.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .helpers import finish_sheet, safe_append

#: Числовые форматы по заголовкам колонок.
_NUM_FMT = {
    "Поездок": "#,##0",
    "Доля, %": "0.0",
    "Пассажиров": "#,##0",
    "Минут": "0.0",
    "Поток, чел/ч": "#,##0",
    "Заполненность, %": "0.0",
}


def _fmt(value: Any) -> Any:
    """Округляет доли до процентов; остальное оставляет как есть."""
    if isinstance(value, float):
        return round(value, 4)
    return value


def passenger_summary_rows(result: Any, network_report: Mapping[str, Any] | None) -> list[list[Any]]:
    """Итоговые числа расчёта пассажиропотока."""
    rows: list[list[Any]] = [
        ["Поездок в городе", round(result.total_demand)],
        ["Поездок на транспорте", round(result.transit_trips)],
        ["Поездок на автомобиле", round(result.car_trips)],
        ["Поездок пешком", round(result.walk_trips)],
        ["Пересадок", round(result.transfer_trips)],
        ["Пешеходный охват, %", round(result.coverage * 100.0, 2)],
        ["Удовлетворённость, %", round(result.satisfaction, 2)],
        ["Снижение от переполнения, п.п.", round(result.overcrowd_penalty_pp, 2)],
    ]
    return rows


def passenger_interval_rows(network_report: Mapping[str, Any] | None) -> list[list[Any]]:
    """Происхождение интервалов — главный разбор «чем мерили»."""
    if not network_report:
        return [["Интервалы", "не загружены"]]
    intervals = network_report.get("intervals", {})
    return [
        ["Маршрутов загружено", network_report.get("routes_loaded", "")],
        ["Маршрутов не загружено", network_report.get("routes_failed", "")],
        ["Интервал измерен перевозчиком", intervals.get("measured", "")],
        ["Интервал ПРЕДПОЛОЖЕН по режиму", intervals.get("assumed_from_mode_default", "")],
        ["Интервала нет", intervals.get("absent", "")],
        ["Доля измеренных, %", round(float(intervals.get("measured_share", 0.0)) * 100.0, 2)],
        ["Примечание", network_report.get("note", "")],
    ]


def passenger_refusal_rows(result: Any) -> list[list[Any]]:
    """Причины отказа и их вклад в потерю."""
    refusals = {k: v for k, v in result.refusals.items() if v > 0}
    total = sum(refusals.values())
    rows: list[list[Any]] = [["Причина", "Поездок", "Доля потерь, %"]]
    for key, value in sorted(refusals.items(), key=lambda item: -item[1]):
        share = (value / total * 100.0) if total > 0 else 0.0
        rows.append([key, round(value), round(share, 2)])
    rows.append(["Итого потеряно", round(total), 100.0 if total > 0 else 0.0])
    return rows


def passenger_mode_rows(result: Any) -> list[list[Any]]:
    """Посадки по режимам — по режиму первой ветки пути (§28 шаг 7)."""
    boardings = getattr(result, "boardings_by_mode", {}) or {}
    rows: list[list[Any]] = [["Режим", "Посадок"]]
    for mode, value in sorted(boardings.items(), key=lambda item: -item[1]):
        rows.append([mode, round(value)])
    return rows


def passenger_load_rows(
    loads: Mapping[Any, float],
    load_factor: Mapping[Any, float],
) -> list[list[Any]]:
    """Заполненность участков: где сеть переполнена, а где пуста.

    Без этого нельзя отличить «переполнение на двух узких местах» от
    «переполнена вся сеть» — а §6.7 требует различать именно это.
    """
    rows: list[list[Any]] = [
        ["Маршрут", "Направление", "Участок", "Поток, чел/ч", "Заполненность, %"]
    ]
    segments = sorted(set(loads) | set(load_factor))
    for segment in segments:
        route_id, direction, index = segment
        rows.append([
            str(route_id),
            direction + 1,
            index + 1,
            round(loads.get(segment, 0.0)),
            round(load_factor.get(segment, 0.0) * 100.0, 2),
        ])
    return rows


def write_passenger_sheet(
    wb: Any,
    result: Any,
    *,
    network_report: Mapping[str, Any] | None = None,
) -> None:
    """Записывает лист «Пассажиропоток»."""
    ws = wb.create_sheet("Пассажиропоток")

    safe_append(ws, ["Пассажиропоток"])
    safe_append(ws, [])

    safe_append(ws, ["Итог"])
    for row in passenger_summary_rows(result, network_report):
        safe_append(ws, row)
    safe_append(ws, [])

    safe_append(ws, ["Происхождение данных"])
    for row in passenger_interval_rows(network_report):
        safe_append(ws, row)
    safe_append(ws, [])

    safe_append(ws, ["Посадки по режимам"])
    for row in passenger_mode_rows(result):
        safe_append(ws, row)
    safe_append(ws, [])

    safe_append(ws, ["Причины отказа"])
    for row in passenger_refusal_rows(result):
        safe_append(ws, row)
    safe_append(ws, [])

    loads = getattr(result, "segment_loads", {}) or {}
    load_factor = getattr(result, "segment_load_factor", {}) or {}
    safe_append(ws, ["Заполненность участков"])
    for row in passenger_load_rows(loads, load_factor):
        safe_append(ws, row)

    headers = [
        "Маршрут", "Направление", "Участок", "Поток, чел/ч", "Заполненность, %",
    ]
    num_fmt = {
        index: fmt for index, header in enumerate(headers, 1)
        if (fmt := _NUM_FMT.get(header))
    }
    finish_sheet(ws, headers, num_fmt=num_fmt, fixed_widths=[18, 12, 10, 16, 18])


__all__ = [
    "passenger_interval_rows",
    "passenger_load_rows",
    "passenger_mode_rows",
    "passenger_refusal_rows",
    "passenger_summary_rows",
    "write_passenger_sheet",
]
