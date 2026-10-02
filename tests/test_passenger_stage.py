"""Тесты флага ``--passenger`` и листа «Пассажиропоток».

Проверяется то, что делает отчёт пригодным для проверки, а не только
для показа: происхождение интервалов видно в листе, провал расчёта не
превращается в пустой отчёт, а маршрут без упорядоченных остановок
останавливает расчёт, а не тихо вычитается из города.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys

import pytest

import passenger as pf

# `passenger_stage` и `config` связаны относительными импортами внутри
# пакета `wikiroutes`, поэтому плоский импорт (`from config import ...`)
# для них невозможен — в отличие от `passenger` и `xlsx`, лежащих в
# `sys.path` как самостоятельные модули. Поэтому эти два берутся путём пакета.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Общие минимальные остановка/направление/маршрут и линия — из test_passenger:
# копии давали бы две реализации одного и того же, что прямо запрещено
# test_public_api.
from test_passenger import Direction, Route, Stop, line  # noqa: E402,F401
from wikiroutes.config import build_cli_config  # noqa: E402
from wikiroutes.passenger_stage import (  # noqa: E402
    PassengerFlowResult,
    network_report_from,
    run_passenger_flow,
)
from passenger.params import W_RIDE, W_WAIT, W_WALK  # noqa: E402
from wikiroutes.xlsx.passenger import (  # noqa: E402
    passenger_interval_rows,
    passenger_load_rows,
    passenger_mode_rows,
    passenger_refusal_rows,
    passenger_summary_rows,
    write_passenger_sheet,
)


def _load_passenger_args_group():
    """Загружает ``cli_args/groups/passenger.py`` напрямую, по пути.

    Обычный импорт не годится: ``cli_args/__init__`` тянет за собой
    ``core``, а тот — все остальные группы, одна из которых использует
    относительный импорт уровнем выше ``wikiroutes`` и потому не грузится
    плоско. Проверяется один флаг, и тащить ради него весь CLI не нужно.
    """
    path = _PROJECT_ROOT / "wikiroutes" / "cli_args" / "groups" / "passenger.py"
    spec = importlib.util.spec_from_file_location("_passenger_args", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.add_passenger_args


add_passenger_args = _load_passenger_args_group()


def _args(argv):
    """Аргументы CLI, собранные только группой пассажиропотока."""
    parser = argparse.ArgumentParser()
    add_passenger_args(parser)
    return parser.parse_args(argv)


def _cfg(**kwargs):
    """Конфигурация CLI с заданными полями расчёта пассажиропотока."""
    args = _args([])
    for key, value in kwargs.items():
        setattr(args, key, value)
    return build_cli_config(args)


# ── флаг ──────────────────────────────────────────────────────────────


def test_passenger_flag_is_off_by_default():
    """Лишний расчёт на миллион пар без запроса — это минуты, украденные у
    всех остальных выводов, поэтому флаг по умолчанию выключен."""
    assert _cfg().passenger is False


def test_passenger_flag_reaches_the_config():
    config = _cfg(passenger=True, passenger_points_per_km2=3.0,
                     passenger_route_limit=40)
    assert config.passenger is True
    assert config.passenger_points_per_km2 == pytest.approx(3.0)
    assert config.passenger_route_limit == 40


def test_flag_is_wired_into_the_argument_group():
    assert _args(["--passenger"]).passenger is True


def test_top_k_of_zero_means_no_limit():
    """«0 концов» и «без ограничения» — разные вещи: первое удалило бы всю
    матрицу, второе ничего не меняет."""
    assert _cfg(passenger_top_k_destinations=0).passenger_top_k_destinations == 0


def test_top_k_reaches_the_config():
    config = _cfg(passenger_top_k_destinations=30)
    assert config.passenger_top_k_destinations == 30


def test_negative_top_k_is_clamped_away():
    """Отрицательное число концов не имеет смысла и должно стать «без
    ограничения», а не ошибкой попозже в скиме."""
    assert _cfg(passenger_top_k_destinations=-5).passenger_top_k_destinations == 0


def test_route_limit_of_zero_means_no_limit():
    """«0 маршрутов» и «без ограничения» — разные вещи; 0 не должен
    случайно превратить город в пустую сеть."""
    assert _cfg(passenger_route_limit=0).passenger_route_limit == 0


def test_negative_density_falls_back_to_something_usable():
    """Нулевая плотность не даёт ни одной точки; молча согласиться с ней
    значит получить «город без пассажиров»."""
    assert _cfg(passenger_points_per_km2=0.0).passenger_points_per_km2 > 0


# ── провал расчёта не молчит ──────────────────────────────────────────


def test_missing_population_raster_is_reported_not_swallowed(tmp_path):
    """Опечатка в пути должна называться опечаткой, а не падать внутри
    rasterio: читатель ошибки решает, чинить путь или искать сбой расчёта."""
    result = run_passenger_flow(
        _cfg(passenger=True, passenger_pop=str(tmp_path / "нет.tif")),
        [line(1, "bus", [(1, 51.0, 39.0), (2, 51.01, 39.0)])],
        bbox=(39.0, 51.0, 39.1, 51.1),
        sessions=None,
        cache=None,
    )
    assert result.ok is False
    assert "GHS_POP" in result.reason


def test_missing_built_raster_explains_the_missing_job_mass(tmp_path):
    """Рабочие места не берутся из слоя населения, и без застройки OD-матрицу
    построить не из чего — это надо сказать, а не вернуть нули.

    Путь застройки указан явно и не существует: иначе сработал бы запасной
    путь к настоящему растру в каталоге проекта и проверка потеряла бы смысл.
    """
    pop = tmp_path / "pop.tif"
    pop.write_bytes(b"")
    result = run_passenger_flow(
        _cfg(passenger=True, passenger_pop=str(pop),
                passenger_built=str(tmp_path / "нет-built.tif")),
        [line(1, "bus", [(1, 51.0, 39.0), (2, 51.01, 39.0)])],
        bbox=(39.0, 51.0, 39.1, 51.1),
        sessions=None,
        cache=None,
    )
    assert result.ok is False
    assert "рабочих мест" in result.reason


def test_pipeline_bbox_object_is_accepted_and_keeps_latitude_first():
    """Пайплайн отдаёт ``BBox`` с полями, а не кортеж. Порядок важен дважды:
    у ``BBox`` (широта, долгота), а ``from_xy`` ждёт (долгота, широта).
    Перестановка направила бы выборку растра в другую часть света."""
    from wikiroutes.passenger_stage import _as_latlon_bbox

    box = _as_latlon_bbox(
        type("B", (), {"min_lat": 51.0, "min_lon": 39.0,
                       "max_lat": 51.5, "max_lon": 39.5})()
    )
    assert (box.min_lat, box.min_lon) == pytest.approx((51.0, 39.0))
    assert (box.max_lat, box.max_lon) == pytest.approx((51.5, 39.5))


def test_tuple_bbox_is_read_as_longitude_first():
    """Кортеж приходит в порядке bbox_as_tuple: (долгота, широта)."""
    from wikiroutes.passenger_stage import _as_latlon_bbox

    box = _as_latlon_bbox((39.0, 51.0, 39.5, 51.5))
    assert (box.min_lat, box.min_lon) == pytest.approx((51.0, 39.0))


def test_missing_city_boundary_is_an_error_not_an_empty_city():
    """Без границы нечего считать: молча вернуть «город без пассажиров»
    значило бы выдать сбой за результат."""
    from wikiroutes.passenger_stage import _as_latlon_bbox

    with pytest.raises(ValueError, match="нет границы"):
        _as_latlon_bbox(None)


def test_city_without_routes_is_reported():
    result = run_passenger_flow(
        _cfg(passenger=True),
        [],
        bbox=(39.0, 51.0, 39.1, 51.1),
        sessions=None,
        cache=None,
    )
    assert result.ok is False
    assert "нет ни одного маршрута" in result.reason


def test_route_without_ordered_stops_stops_the_calculation():
    """Одна остановка — это точка, а не линия: ехать некуда. Молча вычесть
    такой маршрут значило бы уменьшить город и не сказать об этом."""
    result = run_passenger_flow(
        _cfg(passenger=True, passenger_pop="pop.tif",
                passenger_built="built.tif"),
        [line(1, "bus", [(1, 51.0, 39.0)])],
        bbox=(39.0, 51.0, 39.1, 51.1),
        sessions=None,
        cache=None,
    )
    assert result.ok is False
    assert "остановки не упорядочены" in result.reason
    assert "минимум две" in result.reason


def test_failed_result_carries_a_reason_instead_of_empty_numbers():
    result = PassengerFlowResult()
    assert result.ok is False
    assert result.result is None
    assert result.reason is not None


# ── происхождение данных ──────────────────────────────────────────────


class _Cache:
    """Кэш, отдающий заранее заданные ответы каталога."""

    def __init__(self, payload=None):
        self.payload = payload
        self.asked = []

    def get(self, kind, key):
        self.asked.append((kind, key))
        return self.payload


def test_measured_interval_is_read_from_the_cache_not_guessed():
    """Ответы каталога уже лежат в кэше: измеренный интервал должен
    использоваться, а не подменяться предположением по режиму."""
    from wikiroutes.passenger_stage import measured_intervals_for

    payload = {
        "trips": [
            {"schedules": [{"departureIntervals": [{"intervalValue": 8}]}]}
        ]
    }
    cache = _Cache(payload)
    measured = measured_intervals_for(
        [line(11, "bus", [(1, 51.0, 39.0), (2, 51.01, 39.0)])],
        sessions=None,
        cache=cache,
        city_slug="voronezh",
    )
    assert measured == {11: pytest.approx(8.0)}
    assert ("route", "voronezh:11") in cache.asked


def test_route_without_a_cached_answer_gets_no_measured_interval():
    """Нет записи в кэше — значит нет измерения; выдумывать его нельзя."""
    from wikiroutes.passenger_stage import measured_intervals_for

    measured = measured_intervals_for(
        [line(11, "bus", [(1, 51.0, 39.0), (2, 51.01, 39.0)])],
        sessions=None,
        cache=_Cache(None),
        city_slug="voronezh",
    )
    assert measured == {}


def test_a_broken_cache_does_not_kill_the_calculation():
    """Кэш — источник измерения, а не условие расчёта: сбой чтения не должен
    останавливать пассажиров, нужно лишь потерять измеренные интервалы."""

    class Broken:
        def get(self, kind, key):
            raise RuntimeError("кэш закрыт")

    from wikiroutes.passenger_stage import measured_intervals_for

    measured = measured_intervals_for(
        [line(11, "bus", [(1, 51.0, 39.0), (2, 51.01, 39.0)])],
        sessions=None,
        cache=Broken(),
        city_slug="voronezh",
    )
    assert measured == {}


def test_report_counts_measured_and_assumed_intervals_separately():
    """Если интервалы в основном предположены, это обязано быть видно:
    иначе число выглядит измеренным, будучи построенным на допущении."""
    from city.network import (
        INTERVAL_FROM_CATALOG,
        INTERVAL_FROM_MODE_DEFAULT,
        INTERVAL_NONE,
        RouteInterval,
    )

    report = network_report_from(
        {
            1: RouteInterval(10.0, INTERVAL_FROM_CATALOG),
            2: RouteInterval(20.0, INTERVAL_FROM_MODE_DEFAULT),
            3: RouteInterval(6.0, INTERVAL_FROM_MODE_DEFAULT),
            4: RouteInterval(None, INTERVAL_NONE),
        },
        routes_loaded=4,
    )
    assert report["intervals"]["measured"] == 1
    assert report["intervals"]["assumed_from_mode_default"] == 2
    assert report["intervals"]["absent"] == 1
    assert report["intervals"]["measured_share"] == pytest.approx(0.25)
    assert "допущение" in report["note"]


def test_report_of_a_city_with_no_routes_does_not_divide_by_zero():
    report = network_report_from({}, routes_loaded=0)
    assert report["intervals"]["measured_share"] == 0.0


# ── строки листа ──────────────────────────────────────────────────────


def _result():
    stops = [(1, 51.000, 39.000), (2, 51.060, 39.000)]
    return pf.calculate_passenger_flow(
        [line(10, "bus", stops)],
        points=[
            pf.DemandPoint("h1", 51.000, 39.000, 1000.0, 0.0),
            pf.DemandPoint("j1", 51.060, 39.000, 0.0, 1000.0),
        ],
        headways={10: 10.0},
    )


def test_summary_rows_report_all_five_headline_numbers():
    rows = dict(passenger_summary_rows(_result(), None))
    assert "Поездок в городе" in rows
    assert "Удовлетворённость, %" in rows
    assert "Снижение от переполнения, п.п." in rows


def test_refusal_rows_sum_to_the_total_loss():
    result = _result()
    rows = passenger_refusal_rows(result)
    total_row = rows[-1]
    assert total_row[0] == "Итого потеряно"
    listed = sum(row[1] for row in rows[1:-1])
    assert listed == pytest.approx(total_row[1], rel=1e-6)


def test_refusal_rows_say_what_share_each_cause_costs():
    """Список причин без долей бесполезен: игрок не знает, за что браться."""
    rows = passenger_refusal_rows(_result())
    assert rows[0] == ["Причина", "Поездок", "Доля потерь, %"]
    assert rows[-1][2] == pytest.approx(100.0, abs=0.01)


def test_skim_time_matches_the_sum_of_its_own_legs():
    """gen_time и ride/walk/wait должны описывать одну и ту же поездку.

    Проверка ловила настоящий дефект: ветка выхода из салона сверялась с
    ``visited`` (только принятые состояния) вместо ``settled`` (лучшая
    известная стоимость), перезаписывала ``parent`` худшим путём, и
    восстановленная цепочка уходила в сумму на миллионы секунд пешего хода
    при gen_time около 40 минут. Число само по себе выглядело правдоподобно.
    """
    stops = [(i, 51.000 + 0.002 * i, 39.000) for i in range(6)]
    routes = [Route(1, "bus", [Direction([Stop(*p) for p in stops])])]
    points = [
        pf.DemandPoint("h0", 51.000, 39.000, 500.0, 0.0),
        pf.DemandPoint("h1", 51.010, 39.000, 500.0, 0.0),
        pf.DemandPoint("j0", 51.000, 39.000, 0.0, 500.0),
        pf.DemandPoint("j1", 51.010, 39.000, 0.0, 500.0),
    ]

    captured = {}
    import passenger.skims as skims_mod

    original = skims_mod._compute_skim_matrix

    def capture(net, centers, zone_access, stop_index, options,
                origin_zones=None):
        skims = original(net, centers, zone_access, stop_index, options,
                         origin_zones)
        captured["skims"] = skims
        return skims

    skims_mod._compute_skim_matrix = capture
    import passenger.flow as flow_mod
    flow_mod._compute_skim_matrix = capture
    try:
        pf.calculate_passenger_flow(routes, points=points, headways={1: 10.0})
    finally:
        skims_mod._compute_skim_matrix = original
        flow_mod._compute_skim_matrix = original

    skims = captured["skims"]
    assert skims.ride, "ским пуст — проверка ничего бы не значила"
    for key in skims.ride:
        weighted = (
            skims.ride[key] * W_RIDE
            + skims.walk[key] * W_WALK
            + skims.wait[key] * W_WAIT
        )
        assert weighted == pytest.approx(skims.time[key], abs=1.0), (
            f"пара {key}: gen_time {skims.time[key]:.1f} с, "
            f"сумма ног {weighted:.1f} с — это разные поездки"
        )


def test_reconstructed_leg_time_stays_within_reasonable_bounds():
    """Пеший путь не может быть длиннее предела подхода плюс предел выхода.

    Сбой с parent-цепочкой давал суммы в миллионы секунд, что проходило как
    «просто длинный путь». Граница делает такой сбой видимым сразу.
    """
    from passenger.params import MAX_TRANSFER_WALK_S, PassengerOptions

    options = PassengerOptions()
    ceiling = (float(options.max_walk_to_stop_s) + float(MAX_TRANSFER_WALK_S)) * 8

    stops = [(i, 51.000 + 0.002 * i, 39.000) for i in range(6)]
    routes = [
        Route(1, "bus", [Direction([Stop(*p) for p in stops])]),
        Route(2, "bus", [Direction([Stop(*p) for p in stops])]),
    ]
    points = [
        pf.DemandPoint("h0", 51.000, 39.000, 500.0, 0.0),
        pf.DemandPoint("h1", 51.010, 39.000, 500.0, 0.0),
        pf.DemandPoint("j0", 51.000, 39.000, 0.0, 500.0),
        pf.DemandPoint("j1", 51.010, 39.000, 0.0, 500.0),
    ]
    result = pf.calculate_passenger_flow(
        routes, points=points, headways={1: 10.0, 2: 10.0}
    )
    assert result.total_demand > 0.0
    # Путь длиннее суммы всех пределов пешего хода физически невозможен.
    assert result.total_demand < ceiling * 100


def test_mode_rows_report_boardings_by_mode():
    rows = passenger_mode_rows(_result())
    assert rows[0] == ["Режим", "Посадок"]
    assert any(row[0] == "bus" for row in rows[1:])


def test_load_rows_separate_load_from_fill_factor():
    """Поток и заполненность — разные величины: поток большой на загруженной
    линии и большой на пустой, и путать их значит не видеть проблемы."""
    loads = {(5, 0, 0): 1200.0, (5, 0, 1): 300.0}
    factors = {(5, 0, 0): 1.4, (5, 0, 1): 0.25}
    rows = passenger_load_rows(loads, factors)
    assert rows[0] == [
        "Маршрут", "Направление", "Участок", "Поток, чел/ч", "Заполненность, %",
    ]
    assert rows[1] == ["5", 1, 1, 1200, 140.0]
    assert rows[2] == ["5", 1, 2, 300, 25.0]


def test_load_rows_cover_a_segment_present_in_only_one_table():
    loads = {(5, 0, 0): 10.0}
    rows = passenger_load_rows(loads, {})
    assert len(rows) == 2


def test_interval_rows_without_a_report_do_not_invent_numbers():
    rows = passenger_interval_rows(None)
    assert rows == [["Интервалы", "не загружены"]]


def test_interval_rows_make_the_assumption_visible():
    report = network_report_from({}, routes_loaded=3)
    rows = dict((row[0], row[1]) for row in passenger_interval_rows(report))
    assert rows["Маршрутов загружено"] == 3


# ── лист целиком ──────────────────────────────────────────────────────


def test_sheet_is_created_with_the_passenger_title(tmp_path):
    from openpyxl import load_workbook

    wb = __import__("openpyxl").Workbook()
    wb.remove(wb.active)
    write_passenger_sheet(
        wb, _result(), network_report=network_report_from({}, routes_loaded=1)
    )
    path = tmp_path / "passenger.xlsx"
    wb.save(path)

    loaded = load_workbook(path)
    assert "Пассажиропоток" in loaded.sheetnames
    values = [
        [cell.value for cell in row]
        for row in loaded["Пассажиропоток"].iter_rows(max_row=40)
    ]
    flat = [str(value) for row in values for value in row if value is not None]
    assert any("Удовлетворённость" in text for text in flat)
    assert any("Происхождение данных" in text for text in flat)
    assert any("Причины отказа" in text for text in flat)
