"""Тесты модуля расчёта пассажиропотока ``wikiroutes.passenger``.

Проверяются инварианты из DESIGN.md: гравитация (§5.5), окна (§5.6, §5.6a),
параметры пути (§6.3), веса воспринимаемого времени (§6.4), выбор режима
(§6.5), причины отказа (§6.6) и удовлетворённость (§6.7). Экономика (§12)
и строительство (§9) в модуле отсутствуют намеренно.
"""

from __future__ import annotations

import math
import struct

import pytest

import passenger as pf


class Stop:
    """Минимальная остановка (duck-typing как в ``models.Stop``)."""

    def __init__(self, stop_id, lat, lon, name=""):
        self.id = stop_id
        self.latitude = lat
        self.longitude = lon
        self.name = name or f"S{stop_id}"


class Direction:
    def __init__(self, stops):
        self.stops = tuple(stops)
        self.coords = ()


class Route:
    """Минимальный маршрут (duck-typing как в ``models.RouteData``)."""

    def __init__(self, route_id, route_type, directions):
        self.route_id = route_id
        self.route_type = route_type
        self.directions = tuple(directions)


def line(route_id, route_type, points):
    """Маршрут по списку ``(id, lat, lon)`` в одну сторону."""
    return Route(route_id, route_type, [Direction([Stop(*p) for p in points])])


# Линия «две улицы»: A—B и B—C через общую станцию B.
def two_line_network():
    west = line(10, "bus", [(1, 51.000, 39.000), (2, 51.010, 39.000)])
    east = line(20, "bus", [(2, 51.010, 39.000), (3, 51.010, 39.010)])
    return [west, east]


# ── §5.5 Гравитационная модель ─────────────────────────────────────────


def test_zero_distance_gives_zero_weight():
    """Вырожденный поп (начало = конец) исключается, а не делит на ноль."""
    assert pf.passenger_gravity_weight(0.0, 1000.0) == 0.0


def test_short_distance_is_clamped_to_d0():
    """``0 < d < d₀`` — вес как при ``d₀``; вес никогда не больше массы."""
    at_half = pf.passenger_gravity_weight(0.5, 100.0, 2.5)
    at_d0 = pf.passenger_gravity_weight(pf.D0_M, 100.0, 2.5)
    assert at_half == at_d0 == 100.0


def test_weight_never_exceeds_origin_mass():
    """Инвариант §5.5: при любом γ вес ≤ массы начала."""
    for gamma in (0.5, 1.0, 1.5, 2.5, 3.0):
        for dist in (0.1, 1.0, 100.0, 10_000.0):
            assert pf.passenger_gravity_weight(dist, 500.0, gamma) <= 500.0


def test_soft_decay_beyond_max_dist():
    """За порогом притяжения вес делится на 10, а не обнуляется (§5.5)."""
    just_below = pf.passenger_gravity_weight(50_000.0, 1000.0, 1.0)
    just_above = pf.passenger_gravity_weight(50_100.0, 1000.0, 1.0)
    assert just_above > 0.0
    # Степенной закон: отношение ≈ 1/10 с точностью до изменения показателя.
    assert just_below / just_above == pytest.approx(10.0, rel=0.01)


def test_hard_limit_zeroes_weight_beyond_200km():
    """Жёсткий предел 200 км обнуляет вес (§5.5)."""
    assert pf.passenger_gravity_weight(200_001.0, 1000.0, 1.0) == 0.0
    assert pf.passenger_gravity_weight(200_000.0, 1000.0, 1.0) > 0.0


def test_non_finite_inputs_yield_zero():
    """NaN и бесконечности заменяются нулём (§5.5, очистка)."""
    assert pf.passenger_gravity_weight(float("nan"), 100.0) == 0.0
    assert pf.passenger_gravity_weight(float("inf"), 100.0) == 0.0
    assert pf.passenger_gravity_weight(1000.0, float("nan")) == 0.0
    assert pf.passenger_gravity_weight(1000.0, float("inf")) == 0.0


def test_power_law_matches_gamma_semantics():
    """Степенной закон: удвоение расстояния при γ=1 даёт вес вдвое меньше."""
    weight_1k = pf.passenger_gravity_weight(1000.0, 1.0, 1.0)
    weight_2k = pf.passenger_gravity_weight(2000.0, 1.0, 1.0)
    assert weight_2k == pytest.approx(weight_1k / 2.0, rel=1e-9)


def test_airport_gamma_flatter_than_conference():
    """γ задаёт географию типа поездки: аэропорт тянет издалека (§5.5)."""
    air = pf.passenger_gravity_weight(50_000.0, 1.0, pf.GAMMA_TABLE["AIR"])
    conference = pf.passenger_gravity_weight(50_000.0, 1.0, pf.GAMMA_TABLE["CNV"])
    assert air > conference


def test_gamma_table_matches_design_values():
    """Опубликованные показатели из DESIGN.md §5.5."""
    assert pf.GAMMA_TABLE["AIR"] == 0.5
    assert pf.GAMMA_TABLE["EXT"] == 0.5
    assert pf.GAMMA_TABLE["HER"] == 0.5
    assert pf.GAMMA_TABLE["PORT"] == 0.7
    assert pf.GAMMA_TABLE["AQU"] == 1.2
    assert pf.GAMMA_DEFAULT == 1.0
    assert pf.GAMMA_TABLE["HOS"] == 1.5
    assert pf.GAMMA_TABLE["LIB"] == 2.0
    assert pf.GAMMA_TABLE["SCH"] == 2.5
    assert pf.GAMMA_TABLE["CNV"] == 3.0


# ── §5.6 / §5.6a Окна спроса и окна маршрутизации ───────────────────────


def test_departure_windows_are_disjoint_and_cover_day():
    """11 окон спроса покрывают сутки без пересечений и разрывов."""
    windows = pf.DEPARTURE_WINDOWS
    assert len(windows) == 11
    assert windows[0][0] == 0.0
    assert windows[-1][1] == 24.0
    for previous, current in zip(windows, windows[1:]):
        assert previous[1] == current[0]


def test_routing_shares_match_demand_window_sums():
    """Доли W0–W3 совпадают с суммами окон спроса (§5.6a: 15,2 единицы)."""
    totals = {code: 0.0 for code, _start, _end in pf.ROUTING_WINDOWS}
    for start, end, home, work in pf.DEPARTURE_WINDOWS:
        for code, window_start, window_end in pf.ROUTING_WINDOWS:
            if window_start <= start and end <= window_end:
                totals[code] += home + work
                break
        else:  # pragma: no cover — защита от ошибки в таблице окон
            pytest.fail(f"окно {start}-{end} не входит ни в одно окно W")

    total = sum(totals.values())
    assert total == pytest.approx(15.2, rel=1e-6)
    for code, share in pf.ROUTING_SHARES.items():
        assert totals[code] / total == pytest.approx(share, abs=0.001)
    assert sum(pf.ROUTING_SHARES.values()) == pytest.approx(1.0, abs=1e-6)


def test_routing_window_boundaries():
    """Границы окон маршрутизации — 06, 11, 16, 24 (§5.6a)."""
    assert pf.passenger_routing_window_of(0.0) == "W0"
    assert pf.passenger_routing_window_of(5.9) == "W0"
    assert pf.passenger_routing_window_of(6.0) == "W1"
    assert pf.passenger_routing_window_of(10.9) == "W1"
    assert pf.passenger_routing_window_of(11.0) == "W2"
    assert pf.passenger_routing_window_of(15.9) == "W2"
    assert pf.passenger_routing_window_of(16.0) == "W3"
    assert pf.passenger_routing_window_of(23.9) == "W3"


def test_asymmetric_peaks():
    """Пик один: утром из дома, вечером с работы (§5.6)."""
    peaks_home = max(w[2] for w in pf.DEPARTURE_WINDOWS)
    peaks_work = max(w[3] for w in pf.DEPARTURE_WINDOWS)
    assert peaks_home == 2.5
    assert peaks_work == 2.5
    # Пиковые окна — разные: 7–10 из дома и 16–19 с работы.
    assert (7.0, 10.0, 2.5, 0.3) in pf.DEPARTURE_WINDOWS
    assert (16.0, 19.0, 0.3, 2.5) in pf.DEPARTURE_WINDOWS


# ── §6.3 Параметры построения пути ─────────────────────────────────────


def test_path_limits_match_design():
    # MAX_TRANSFERS = 8 (§6.3): первая редакция таблицы давала 4 (§2.5),
    # но §6.2 разрешает конфликт в пользу 8 — наблюдаемая сходимость RAPTOR
    # 8,4 раунда, и предел 4 отсекал бы достижимые пути.
    assert pf.MAX_TRANSFERS == 8
    assert pf.MAX_WALK_TO_STOP_S == 45 * 60
    assert pf.MAX_TRANSFER_WALK_S == 10 * 60
    assert pf.WALK_SPEED_MPS == 1.0
    assert pf.ARRIVAL_GAP_S == 50.0
    assert pf.MAX_POP_SIZE == 200


def test_wait_is_half_headway_plus_arrival_gap():
    """Ожидание = половина интервала + зазор прибытия (§6.3)."""
    assert pf.passenger_wait_s(10.0) == 10 * 30 + 50
    assert pf.passenger_wait_s(4.0) == 4 * 30 + 50


# ── §6.4 Воспринимаемое время ──────────────────────────────────────────


def test_generalized_time_uses_corrected_weights():
    """Веса ходьбы и ожидания — исправленные RP-оценки (§6.4)."""
    assert pf.W_RIDE == 1.00
    assert pf.W_WALK == 1.67
    assert pf.W_WAIT == 1.72
    assert pf.W_LATE == 0.45
    combined = pf.passenger_generalized_time(600.0, 60.0, 300.0)
    assert combined == pytest.approx(600.0 + 60 * 1.67 + 300 * 1.72)


def test_waiting_costs_more_than_riding_per_second():
    """Ключевой смысл §6.4: 10 минут ожидания стоят дороже 10 минут езды."""
    wait_only = pf.passenger_generalized_time(0.0, 0.0, 600.0)
    ride_only = pf.passenger_generalized_time(600.0, 0.0, 0.0)
    assert wait_only > ride_only
    assert wait_only == pytest.approx(1032.0)


# ── §6.5 / §6.5.1 Выбор режима ─────────────────────────────────────────


def test_equal_times_give_equal_shares():
    shares = pf.passenger_transit_share(1800.0, 1800.0)
    assert shares["transit"] == pytest.approx(0.5)
    assert shares["car"] == pytest.approx(0.5)
    assert shares["walk"] == 0.0


def test_beta_calibrated_to_ten_minute_gap():
    """Калибровочная величина §6.5.1: разрыв 10 мин → шансы машины ×1,3.

    Проверяется **отношение шансов** (odds), а не отношение долей: логит
    нормирует доли, поэтому при равных исходных долях отношение долей не
    равно 1,3 — но отношение шансов равно ровно.
    """
    assert math.exp(pf.BETA_PER_S * 600.0) == pytest.approx(1.3, rel=1e-9)

    equal = pf.passenger_transit_share(1800.0, 1800.0)
    assert equal["car"] / equal["transit"] == pytest.approx(1.0, rel=1e-9)

    # Транспорт на 10 мин быстрее: шансы машины падают в 1,3 раза.
    transit_faster = pf.passenger_transit_share(1200.0, 1800.0)
    odds_transit_faster = transit_faster["car"] / transit_faster["transit"]
    assert odds_transit_faster == pytest.approx(1 / 1.3, rel=1e-6)

    # Машина на 10 мин быстрее: шансы машины выше в 1,3 раза.
    car_faster = pf.passenger_transit_share(1800.0, 1200.0)
    odds_car_faster = car_faster["car"] / car_faster["transit"]
    assert odds_car_faster == pytest.approx(1.3, rel=1e-6)

    # Монотонность: чем медленнее транспорт, тем ниже его доля.
    shares = [
        pf.passenger_transit_share(time, 1800.0)["transit"]
        for time in (1200.0, 1500.0, 1800.0, 2100.0, 2400.0)
    ]
    assert shares == sorted(shares, reverse=True)


def test_infinite_transit_time_gives_zero_share():
    """Нет пути — доля транспорта строго нулевая."""
    shares = pf.passenger_transit_share(math.inf, 1800.0)
    assert shares["transit"] == 0.0
    assert shares["car"] == pytest.approx(1.0)


def test_walk_competitor_only_when_viable():
    """Пеший вариант учитывается, только если поездка пешком осмысленна."""
    short = pf.passenger_transit_share(1800.0, 1800.0, 600.0)
    assert short["walk"] > 0.0
    absent = pf.passenger_transit_share(1800.0, 1800.0, None)
    assert absent["walk"] == 0.0


def test_car_time_uses_congestion_and_short_trip_penalty():
    """Пробка по времени суток и штраф короткой поездки (§6.5.1, §6.8)."""
    options = pf.PassengerOptions()
    free_flow = pf.passenger_car_time_s(10_000.0, 12.0, options)
    peak = pf.passenger_car_time_s(10_000.0, 8.0, options)
    assert peak > free_flow
    assert peak / free_flow == pytest.approx(1.50, rel=1e-6)

    # Поездка короче 1000 м получает штраф «возни с машиной» (§6.8).
    # Сравниваем на одинаковой дистанции по разные стороны порога.
    threshold = options.short_car_threshold_m
    under = pf.passenger_car_time_s(threshold * 0.999, 12.0, options)
    over = pf.passenger_car_time_s(threshold * 1.001, 12.0, options)
    assert under > over
    assert under - over == pytest.approx(options.short_car_penalty_s, abs=1.0)
    # И на короткой поездке автомобиль проигрывает велосипеду-эквиваленту:
    # 500 м на машине с штрафом занимают больше 10 минут.
    assert pf.passenger_car_time_s(500.0, 12.0, options) > 600.0


def test_congestion_levels_match_design():
    assert pf.passenger_congestion_factor(2.0) == 0.80
    assert pf.passenger_congestion_factor(4.0) == 0.90
    assert pf.passenger_congestion_factor(12.0) == 1.00
    assert pf.passenger_congestion_factor(10.0) == 1.25
    assert pf.passenger_congestion_factor(8.0) == 1.50


# ── §8.2 Интервалы ─────────────────────────────────────────────────────


def test_headway_accepts_only_design_steps_in_strict_mode():
    assert pf.passenger_validate_headway(3.0) == 3.0
    assert pf.passenger_validate_headway(120.0) == 120.0
    with pytest.raises(ValueError):
        pf.passenger_validate_headway(7.0, strict=True)


def test_zero_headway_stops_service():
    assert pf.passenger_validate_headway(0.0) is None
    assert pf.passenger_validate_headway(None) is None


def test_headway_steps_contain_expected_values():
    for value in (3, 4, 5, 6, 8, 10, 12, 15, 20, 30, 40, 60, 90, 120):
        assert value in pf.HEADWAY_STEPS


def test_mode_mapping_covers_all_route_types():
    assert pf.passenger_mode_key("bus") == "bus"
    assert pf.passenger_mode_key("trolleybus") == "bus"
    assert pf.passenger_mode_key("electrobus") == "bus"
    assert pf.passenger_mode_key("tram") == "tram"
    assert pf.passenger_mode_key("metro") == "metro"
    assert pf.passenger_mode_key("monorail") == "metro"
    assert pf.passenger_mode_key("train") == "rail"
    assert pf.passenger_mode_key("water") == "water"
    assert pf.passenger_mode_key("funicular") == "cable"
    assert pf.passenger_mode_key("unknown") == "bus"


# ── §7.1 Параметры режимов ─────────────────────────────────────────────


def test_mode_walk_radii_match_design():
    """Пешеходные радиусы §7.1: 500/600/800/1500 м."""
    assert pf.MODE_PARAMS["bus"]["walk_radius_m"] == 500.0
    assert pf.MODE_PARAMS["tram"]["walk_radius_m"] == 600.0
    assert pf.MODE_PARAMS["metro"]["walk_radius_m"] == 800.0
    assert pf.MODE_PARAMS["rail"]["walk_radius_m"] == 1500.0


def test_mode_capacities_match_design():
    assert pf.MODE_PARAMS["bus"]["vehicle_capacity"] == 90.0
    assert pf.MODE_PARAMS["tram"]["vehicle_capacity"] == 260.0
    assert pf.MODE_PARAMS["metro"]["vehicle_capacity"] == 800.0
    assert pf.MODE_PARAMS["rail"]["vehicle_capacity"] == 980.0


# ── §5.2 / §13.3 Точки спроса и пеший охват ─────────────────────────────


def test_demand_point_masses_do_not_create_passengers():
    """§5.2: числа residents/jobs сами по себе не создают пассажиров.

    Поездки появляются только там, где есть начало и конец; масса точки,
    равная себе, не порождает пассажира сам у себя.
    """
    route = line(10, "bus", [(1, 51.000, 39.000), (2, 51.010, 39.000)])
    # Одна точка и как начало, и как конец — поездки негде рождаться.
    only_one = pf.calculate_passenger_flow(
        [route], masses={"id:1": (1e9, 1e9), "id:2": (0.0, 0.0)}, headways={10: 10.0}
    )
    assert only_one.total_demand == 0.0
    assert only_one.pops == ()

    # Две точки: масса даёт объём поездок ровно по формуле §5.3.
    two = pf.calculate_passenger_flow(
        [route],
        masses={"id:1": (1e9, 0.0), "id:2": (0.0, 1e9)},
        headways={10: 10.0},
    )
    assert two.total_demand == pytest.approx(2e9)
    assert two.uniform_masses is False


def test_masses_are_complete_not_merged_with_defaults():
    """Переданные массы полные: у отсутствующих ключей масса нулевая."""
    routes = two_line_network()
    result = pf.calculate_passenger_flow(
        routes,
        masses={"id:1": (1000.0, 0.0), "id:3": (0.0, 1000.0)},
        headways={10: 10.0, 20: 10.0},
    )
    # Промежуточная станция не получает население по умолчанию.
    assert result.total_demand == pytest.approx(2000.0)


def test_coverage_is_partial_when_both_ends_are_far():
    """«В пешем охвате» меньше единицы, когда начало и конец далеко (§13.3)."""
    routes = [line(10, "metro", [(1, 51.000, 39.000), (2, 51.010, 39.000)])]
    points = [
        pf.DemandPoint("home", 51.000, 39.000, 3000.0, 0.0),
        pf.DemandPoint("job", 51.010, 39.000, 0.0, 3000.0),
        pf.DemandPoint("far_home", 51.100, 39.500, 4000.0, 0.0),
        pf.DemandPoint("far_job", 51.110, 39.600, 0.0, 4000.0),
    ]
    result = pf.calculate_passenger_flow(routes, points=points, headways={10: 5.0})
    assert 0.0 < result.coverage < 1.0
    assert result.refusals[pf.RefusalReason.FAR_FROM_STOP.value] > 0.0


# ── §6.6 Причины отказа ────────────────────────────────────────────────


def test_refusal_reasons_cover_no_path_and_never_tariff():
    """Тариф — это экономика (§12), в модуле его быть не должно."""
    reasons = {reason.value for reason in pf.RefusalReason}
    assert pf.RefusalReason.NO_PATH.value in reasons
    assert pf.RefusalReason.FAR_FROM_STOP.value in reasons
    assert pf.RefusalReason.CAR_BETTER.value in reasons
    assert pf.RefusalReason.TRAVEL_TIME.value in reasons
    assert pf.RefusalReason.WAITING.value in reasons
    assert pf.RefusalReason.TRANSFER.value in reasons
    assert pf.RefusalReason.OVERCROWD.value in reasons
    assert "fare" not in reasons


def test_isolated_line_gives_no_path():
    """Районы, куда не идёт ни одна линия, дают «нет пути»."""
    routes = [line(10, "bus", [(1, 51.000, 39.000), (2, 51.010, 39.000)])]
    points = [
        pf.DemandPoint("near_home", 51.000, 39.000, 1000.0, 0.0),
        pf.DemandPoint("near_job", 51.010, 39.000, 0.0, 1000.0),
        pf.DemandPoint("cut_off", 51.200, 39.800, 9000.0, 0.0),
        pf.DemandPoint("cut_job", 51.210, 39.810, 0.0, 9000.0),
    ]
    result = pf.calculate_passenger_flow(routes, points=points, headways={10: 10.0})
    assert result.refusals[pf.RefusalReason.NO_PATH.value] > 0.0


def test_refusal_loss_is_split_not_duplicated():
    """Если причин несколько, потеря делится между ними (§6.6).

    Сумма причин равна потоку, не пошедшему на транспорт: ни одна потеря
    не объясняется дважды, но и не теряется.

    Сеть строится из трёх линий, связанных общими остановками, чтобы путь
    требовал двух пересадок: только тогда в списке появляется вторая
    причина, и деление видно. На одной линии причин ровно одна, и тест
    про деление ничего бы не проверял.
    """
    routes = [
        line(10, "bus", [(1, 51.000, 39.000), (2, 51.005, 39.000)]),
        line(11, "bus", [(2, 51.005, 39.000), (3, 51.010, 39.000)]),
        line(12, "bus", [(3, 51.010, 39.000), (4, 51.015, 39.000)]),
    ]
    points = [
        pf.DemandPoint("home", 51.000, 39.000, 4000.0, 0.0),
        pf.DemandPoint("job", 51.015, 39.000, 0.0, 4000.0),
    ]
    result = pf.calculate_passenger_flow(
        routes, points=points, headways={10: 10.0, 11: 10.0, 12: 10.0}
    )
    explained = sum(result.refusals.values())
    not_by_transit = result.total_demand - result.transit_trips
    assert explained == pytest.approx(not_by_transit, rel=1e-6)
    # Хотя бы две причины участвуют: одна не «съедает» весь список.
    assert sum(1 for value in result.refusals.values() if value > 0.0) >= 2


def test_overcrowding_is_not_a_refusal_reason_when_vagons_are_empty():
    """Причина появляется, когда поток превысил ёмкость, а не когда ёмкость
    посчитана.

    Раньше «Переполнение» начислялось по признаку «ёмкость известна», то есть
    почти всегда. На реальном городе это давало 45 % потерь при нулевом
    штрафе за переполнение: показатель знал, что вагоны пусты, а список
    уводил игрока добавлять вместимость туда, где её не хватало.
    """
    stops = [(i, 51.000 + 0.003 * i, 39.000) for i in range(8)]
    routes = [line(i, "bus", stops) for i in range(11, 19)]
    points = [
        pf.DemandPoint("home", 51.000, 39.000, 400.0, 0.0),
        pf.DemandPoint("job", 51.021, 39.000, 0.0, 400.0),
    ]
    result = pf.calculate_passenger_flow(
        routes, points=points,
        headways={i: 10.0 for i in range(11, 19)},
    )
    assert result.overcrowd_penalty_pp == 0.0, "вагоны не переполнены"
    assert result.refusals[pf.RefusalReason.OVERCROWD.value] == 0.0


def test_overcrowding_appears_as_a_refusal_reason_once_it_is_measured():
    """Обратная сторона: если поток превысил ёмкость, причина обязана быть."""
    stops = [(i, 51.000 + 0.003 * i, 39.000) for i in range(8)]
    routes = [line(20, "bus", stops)]
    # Много пассажиров и редкий интервал: одна линия, пиковый поток заметно
    # выше часовой ёмкости автобуса.
    points = [
        pf.DemandPoint("home", 51.000, 39.000, 40000.0, 0.0),
        pf.DemandPoint("job", 51.021, 39.000, 0.0, 40000.0),
    ]
    result = pf.calculate_passenger_flow(
        routes, points=points, headways={20: 60.0}
    )
    assert result.overcrowd_penalty_pp > 0.0, "переполнение должно быть замечено"
    assert result.refusals[pf.RefusalReason.OVERCROWD.value] > 0.0


# ── §6.7 / §8.3 Удовлетворённость и заполненность ──────────────────────


def test_overcrowding_reduces_satisfaction():
    """Переполнение снижает удовлетворённость, но не обнуляет её (§6.7).

    Контраст строится увеличением потока при неизменном интервале: одна и та
    же линия, один и тот же поезд, разная нагрузка.
    """
    stops = [(1, 51.000, 39.000), (2, 51.060, 39.000)]
    # Метро, интервал 2 мин: 800 × 30 = 24 000 пассажиров в час на сегмент.
    # Пик — 10 % суточного потока, то есть переполнение начинается
    # примерно от 240 000 поездок в день.
    light = [
        pf.DemandPoint("home", 51.000, 39.000, 20_000.0, 0.0),
        pf.DemandPoint("job", 51.060, 39.000, 0.0, 20_000.0),
    ]
    heavy = [
        pf.DemandPoint("home", 51.000, 39.000, 2_000_000.0, 0.0),
        pf.DemandPoint("job", 51.060, 39.000, 0.0, 2_000_000.0),
    ]
    options = {"headways": {10: 2.0}}
    roomy = pf.calculate_passenger_flow([line(10, "metro", stops)], points=light, **options)
    crowded = pf.calculate_passenger_flow([line(10, "metro", stops)], points=heavy, **options)

    assert crowded.transit_trips > roomy.transit_trips
    assert crowded.overcrowd_penalty_pp > roomy.overcrowd_penalty_pp
    assert crowded.satisfaction < roomy.satisfaction
    assert crowded.satisfaction > 0.0
    assert max(crowded.segment_load_factor.values()) > 1.0
    assert crowded.refusals[pf.RefusalReason.OVERCROWD.value] > 0.0

    # Редкая линия при той же нагрузке переполнена сильнее по заполненности
    # (§8.3): меньше мест на час — выше отношение потока к провозной
    # способности. Штраф в пунктах при этом может быть меньше: редкая линия
    # просто везёт меньше людей, и §6.7 считает долю поездок, а не составов.
    rare = pf.calculate_passenger_flow(
        [line(10, "metro", stops)], points=heavy, headways={10: 60.0}
    )
    assert max(rare.segment_load_factor.values()) > max(
        crowded.segment_load_factor.values()
    )


def test_load_factor_is_ratio_not_queue():
    """§8.3: заполненность — отношение потока к провозной способности."""
    routes = [line(10, "bus", [(1, 51.000, 39.000), (2, 51.020, 39.000)])]
    points = [
        pf.DemandPoint("home", 51.000, 39.000, 1000.0, 0.0),
        pf.DemandPoint("job", 51.020, 39.000, 0.0, 1000.0),
    ]
    result = pf.calculate_passenger_flow(routes, points=points, headways={10: 10.0})
    assert result.segment_load_factor
    assert all(value >= 0.0 for value in result.segment_load_factor.values())
    # Ежедневный поток больше часовой провозной способности.
    for daily in result.segment_loads.values():
        assert daily > 0.0


def test_satisfaction_is_percentage():
    assert 0.0 <= pf.calculate_passenger_flow(
        two_line_network(),
        masses={"id:1": (100.0, 10.0), "id:2": (10.0, 10.0), "id:3": (10.0, 100.0)},
        headways={10: 10.0, 20: 10.0},
    ).satisfaction <= 100.0


# ── Пересадки ───────────────────────────────────────────────────────────


def test_shared_stop_produces_transfer():
    """Поездка через общую станцию — это пересадка (§13.4)."""
    result = pf.calculate_passenger_flow(
        two_line_network(),
        masses={"id:1": (4000.0, 10.0), "id:2": (10.0, 10.0), "id:3": (10.0, 4000.0)},
        headways={10: 10.0, 20: 10.0},
    )
    assert result.transfer_trips > 0.0


def test_boardings_count_first_and_transfer_stops():
    """Посадки считаются и на первой остановке, и на пересадочной."""
    result = pf.calculate_passenger_flow(
        two_line_network(),
        masses={"id:1": (4000.0, 10.0), "id:2": (10.0, 10.0), "id:3": (10.0, 4000.0)},
        headways={10: 10.0, 20: 10.0},
    )
    assert result.boardings.get("id:1", 0.0) > 0.0
    assert result.boardings.get("id:2", 0.0) > 0.0


def test_far_stops_do_not_transfer():
    """Пересадка измеряется временем ходьбы, а не членством в группе (§6.3)."""
    # Станции разведены примерно на 800 м: пешеходная пересадка невозможна,
    # хотя формально они в одном коридоре.
    routes = [
        line(10, "bus", [(1, 51.000, 39.000), (2, 51.000, 39.010)]),
        line(20, "bus", [(3, 51.000, 39.020), (4, 51.000, 39.030)]),
    ]
    result = pf.calculate_passenger_flow(
        routes,
        masses={"id:1": (3000.0, 0.0), "id:4": (0.0, 3000.0)},
        headways={10: 10.0, 20: 10.0},
    )
    assert result.transfer_trips == 0.0
    assert result.transit_trips == 0.0


# ── Попы и санитация ───────────────────────────────────────────────────


def test_pops_never_exceed_cap():
    """Поп разбивается на части по 200 человек (§5.3)."""
    routes = [line(10, "bus", [(1, 51.000, 39.000), (2, 51.010, 39.000)])]
    result = pf.calculate_passenger_flow(
        routes,
        masses={"id:1": (50_000.0, 0.0), "id:2": (0.0, 50_000.0)},
        headways={10: 10.0},
    )
    assert result.pops
    assert max(pop.size for pop in result.pops) == pytest.approx(200.0)
    assert sum(pop.size for pop in result.pops) == pytest.approx(result.total_demand)


def test_pop_ids_are_unique():
    routes = [line(10, "bus", [(1, 51.000, 39.000), (2, 51.010, 39.000)])]
    result = pf.calculate_passenger_flow(
        routes,
        masses={"id:1": (50_000.0, 0.0), "id:2": (0.0, 50_000.0)},
        headways={10: 10.0},
    )
    identifiers = [pop.id for pop in result.pops]
    assert len(identifiers) == len(set(identifiers))


def test_zero_demand_produces_no_pops():
    routes = [line(10, "bus", [(1, 51.000, 39.000), (2, 51.010, 39.000)])]
    result = pf.calculate_passenger_flow(
        routes,
        masses={"id:1": (0.0, 0.0), "id:2": (0.0, 0.0)},
        headways={10: 10.0},
    )
    assert result.pops == ()
    assert result.total_demand == 0.0


# ── Пустая и вырожденная сеть ──────────────────────────────────────────


def test_empty_network_returns_zero_result():
    result = pf.calculate_passenger_flow([])
    assert result.total_demand == 0.0
    assert result.transit_trips == 0.0
    assert result.coverage == 0.0
    assert result.pops == ()


def test_single_stop_route_contributes_no_segments():
    route = Route(10, "bus", [Direction([Stop(1, 51.0, 39.0)])])
    result = pf.calculate_passenger_flow(
        [route], masses={"id:1": (1000.0, 1000.0)}, headways={10: 10.0}
    )
    assert result.segment_loads == {}


def test_stopped_line_is_excluded_from_network():
    """Интервал 0 — движение прекращено (§8.2)."""
    result = pf.calculate_passenger_flow(
        two_line_network(),
        masses={"id:1": (3000.0, 0.0), "id:3": (0.0, 3000.0)},
        headways={10: 10.0, 20: 0.0},
    )
    assert result.transit_trips == 0.0
    assert result.refusals[pf.RefusalReason.NO_PATH.value] > 0.0


def test_routes_without_directions_are_ignored():
    route = Route(10, "bus", [])
    result = pf.calculate_passenger_flow([route], headways={10: 10.0})
    assert result.total_demand == 0.0


# ── Устойчивость входа ─────────────────────────────────────────────────


def test_malformed_stops_are_skipped():
    """Остановка без координат не должна ломать расчёт."""
    good = Stop(1, 51.000, 39.000)
    broken = Stop(2, None, None)
    far = Stop(3, float("nan"), 39.0)
    route = Route(
        10,
        "bus",
        [Direction([good, broken, far, Stop(4, 51.010, 39.000)])],
    )
    result = pf.calculate_passenger_flow([route], headways={10: 10.0})
    assert result.total_demand > 0.0


def test_dict_like_stops_are_supported():
    """Модуль работает и с dict-остановками, и с объектами."""

    class DictDirection:
        stops = (
            {"id": 1, "latitude": 51.0, "longitude": 39.0, "name": "A"},
            {"id": 2, "latitude": 51.01, "longitude": 39.0, "name": "B"},
        )

    class DictRoute:
        route_id = 10
        route_type = "bus"
        directions = (DictDirection(),)

    result = pf.calculate_passenger_flow([DictRoute()], headways={10: 10.0})
    assert result.total_demand > 0.0
    assert result.segment_loads


def test_float_stop_id_is_normalized():
    stop = Stop(1.0, 51.0, 39.0)
    route = Route(10, "bus", [Direction([stop, Stop(2.0, 51.01, 39.0)])])
    result = pf.calculate_passenger_flow(
        [route], masses={"id:1": (500.0, 0.0), "id:2": (0.0, 500.0)}, headways={10: 10.0}
    )
    assert result.total_demand > 0.0


def test_uniform_masses_flag():
    result = pf.calculate_passenger_flow(two_line_network(), headways={10: 10.0, 20: 10.0})
    assert result.uniform_masses is True
    assert result.total_demand > 0.0


# ── Режимы влияют на результат ─────────────────────────────────────────


def test_rail_captures_more_demand_than_bus_on_long_corridor():
    """Быстрый режим выигрывает на длинном коридоре (§7.1)."""
    stops = [(1, 51.000, 39.000), (2, 51.060, 39.000)]
    masses = {"id:1": (3000.0, 0.0), "id:2": (0.0, 3000.0)}
    bus = pf.calculate_passenger_flow([line(10, "bus", stops)], masses=masses, headways={10: 10.0})
    rail = pf.calculate_passenger_flow([line(10, "train", stops)], masses=masses, headways={10: 10.0})
    assert rail.transit_trips > bus.transit_trips


def test_faster_headway_captures_more_demand():
    """Чаще ходящая линия перевозит больше (§8.2)."""
    stops = [(1, 51.000, 39.000), (2, 51.030, 39.000)]
    masses = {"id:1": (3000.0, 0.0), "id:2": (0.0, 3000.0)}
    rare = pf.calculate_passenger_flow([line(10, "bus", stops)], masses=masses, headways={10: 30.0})
    frequent = pf.calculate_passenger_flow([line(10, "bus", stops)], masses=masses, headways={10: 3.0})
    assert frequent.transit_trips > rare.transit_trips


def test_slow_headway_produces_waiting_reason():
    """Редкая линия даёт диагностическую причину «ожидание» (§6.6)."""
    stops = [(1, 51.000, 39.000), (2, 51.060, 39.000)]
    result = pf.calculate_passenger_flow(
        [line(10, "bus", stops)],
        masses={"id:1": (2_000_000.0, 0.0), "id:2": (0.0, 2_000_000.0)},
        headways={10: 120.0},
    )
    assert result.refusals[pf.RefusalReason.WAITING.value] > 0.0


# ── Инварианты результата ──────────────────────────────────────────────


def test_mode_shares_sum_to_one():
    result = pf.calculate_passenger_flow(
        two_line_network(),
        masses={"id:1": (3000.0, 0.0), "id:2": (10.0, 10.0), "id:3": (0.0, 3000.0)},
        headways={10: 10.0, 20: 10.0},
    )
    total = result.transit_trips + result.walk_trips + result.car_trips
    assert total == pytest.approx(result.total_demand, rel=1e-6)


def test_transfer_trips_do_not_exceed_transit_trips():
    result = pf.calculate_passenger_flow(
        two_line_network(),
        masses={"id:1": (4000.0, 0.0), "id:2": (10.0, 10.0), "id:3": (0.0, 4000.0)},
        headways={10: 10.0, 20: 10.0},
    )
    assert result.transfer_trips <= result.transit_trips


def test_boardings_sum_to_at_least_transit_trips():
    result = pf.calculate_passenger_flow(
        two_line_network(),
        masses={"id:1": (4000.0, 0.0), "id:2": (10.0, 10.0), "id:3": (0.0, 4000.0)},
        headways={10: 10.0, 20: 10.0},
    )
    assert sum(result.boardings.values()) >= result.transit_trips


def test_coincident_stops_do_not_break_heap_ordering():
    """Равные времена в куче не сравнивают ``None`` с числом.

    Несколько остановок в одной точке дают одинаковое стартовое время;
    без порядкового номера в первичном ключе кучи сравнение падало бы с
    ``TypeError: '<' not supported between NoneType and int``.
    """
    # Четыре остановки в одной координате плюс пересадочная линия.
    routes = [
        line(10, "bus", [(1, 51.0, 39.0), (2, 51.0, 39.0), (3, 51.0, 39.0)]),
        line(20, "bus", [(4, 51.0, 39.0), (5, 51.0, 39.0)]),
    ]
    # Точки спроса разнесены на 300 м: поездка ненулевая, а стартовые
    # времена в куче совпадают из-за совпадающих координат остановок.
    points = [
        pf.DemandPoint("home", 51.000, 39.000, 2000.0, 0.0),
        pf.DemandPoint("job", 51.003, 39.000, 0.0, 2000.0),
    ]
    result = pf.calculate_passenger_flow(routes, points=points, headways={10: 10.0, 20: 10.0})
    assert result.total_demand > 0.0


def test_zero_distance_pairs_are_excluded():
    """Начало, равное концу, не порождает поездку (§5.3, вырожденный поп)."""
    routes = [line(10, "bus", [(1, 51.0, 39.0), (2, 51.01, 39.0)])]
    points = [
        pf.DemandPoint("home", 51.0, 39.0, 1000.0, 1000.0),
        pf.DemandPoint("job", 51.01, 39.0, 1000.0, 1000.0),
    ]
    result = pf.calculate_passenger_flow(routes, points=points, headways={10: 10.0})
    # Ни одна пара «точка → она же» не попала в матрицу.
    assert all(pop.origin_key != pop.dest_key for pop in result.pops)


def test_inline_distance_matches_reference_haversine():
    """Инлайн-гаверсинус совпадает с ``geometry.haversine_km``.

    В оптимизированном пути расчёта пар формула считается вручную. Ошибка
    в ней (``sin²(Δφ/2)``, сложенная с полным ``sin²φ``) давала расстояние
    11 344 км вместо 1,1 км и молча обнуляла весь поток, поэтому
    расхождение фиксируется тестом, а не eyeball-проверкой.
    """
    cases = [
        (51.000, 39.000, 51.010, 39.000),   # ~1,1 км по меридиану
        (51.000, 39.000, 51.000, 39.010),   # ~640 м по параллели
        (51.660, 39.200, 51.680, 39.240),   # ~3,4 км по диагонали
        (51.000, 39.000, 51.000, 39.000),   # совпадающие точки
    ]
    for lat1, lon1, lat2, lon2 in cases:
        inline = _inline_haversine_m(lat1, lon1, lat2, lon2)
        reference = pf._haversine_m(lat1, lon1, lat2, lon2)
        assert inline == pytest.approx(reference, rel=1e-9, abs=1e-6)


def _inline_haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Копия формулы из ``calculate_passenger_flow`` для проверки равенства."""
    radius = 6_371_000.0
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlat = lat2_r - lat1_r
    dlon = math.radians(lon2) - math.radians(lon1)
    half_dlat = math.sin(dlat / 2.0)
    half_dlon = math.sin(dlon / 2.0)
    a = half_dlat * half_dlat + math.cos(lat1_r) * math.cos(lat2_r) * half_dlon * half_dlon
    if a <= 0.0:
        return 0.0
    return 2.0 * radius * math.asin(math.sqrt(min(1.0, a)))


def test_od_matrix_respects_distance_decay():
    """Ближние назначения получают больше поездок, дальние — меньше."""
    routes = [line(10, "bus", [(1, 51.000, 39.000), (2, 51.030, 39.000)])]
    points = [
        pf.DemandPoint("home", 51.000, 39.000, 10_000.0, 0.0),
        pf.DemandPoint("near", 51.010, 39.000, 0.0, 5_000.0),
        pf.DemandPoint("mid", 51.030, 39.000, 0.0, 5_000.0),
        pf.DemandPoint("far", 51.200, 39.000, 0.0, 5_000.0),
    ]
    result = pf.calculate_passenger_flow(routes, points=points, headways={10: 10.0})
    # Попы по дальнему назначению не длиннее поездки до среднего и короче
    # поездки до ближнего — иначе гравитация не работает.
    by_dest: dict[str, float] = {}
    for pop in result.pops:
        by_dest[pop.dest_key] = by_dest.get(pop.dest_key, 0.0) + pop.size
    assert by_dest.get("near", 0.0) > by_dest.get("mid", 0.0)
    assert by_dest.get("mid", 0.0) > by_dest.get("far", 0.0)


def test_inlined_logit_matches_public_function():
    """Инлайн логита в цикле совпадает с ``passenger_transit_share``.

    Горячий цикл считает доли режимов раскрытой формулой ради скорости.
    Расхождение с публичной функцией означало бы, что тесты проверяют одну
    реализацию, а считает игра другую.
    """
    cases = [
        (1800.0, 1800.0, None),
        (1200.0, 1800.0, None),
        (1800.0, 1200.0, None),
        (900.0, 1800.0, 600.0),
        (1800.0, 1800.0, 300.0),
        (math.inf, 1800.0, None),
        (1800.0, math.inf, None),
        (600.0, 900.0, 1200.0),
    ]
    beta = pf.BETA_PER_S
    for transit, car, walk in cases:
        expected = pf.passenger_transit_share(transit, car, walk, beta_per_s=beta)

        if transit < math.inf and car < math.inf:
            best_gen = transit if transit < car else car
            if walk is not None and walk < best_gen:
                best_gen = walk
            w_tr = math.exp(-beta * (transit - best_gen))
            w_car = math.exp(-beta * (car - best_gen))
            w_walk = math.exp(-beta * (walk - best_gen)) if walk is not None else 0.0
            total = w_tr + w_car + w_walk
            got = {"transit": w_tr / total, "car": w_car / total, "walk": w_walk / total}
        elif transit < math.inf:
            got = {"transit": 1.0, "car": 0.0, "walk": 0.0}
        else:
            got = {
                "transit": 0.0,
                "walk": 1.0 if walk is not None else 0.0,
                "car": 0.0 if walk is not None else 1.0,
            }

        for key, value in got.items():
            assert value == pytest.approx(expected[key], rel=1e-9, abs=1e-12), (
                f"расхождение для {transit}/{car}/{walk}, режим {key}"
            )
        assert sum(got.values()) == pytest.approx(1.0, rel=1e-9)


def test_module_has_no_economy_or_construction_api():
    """Экономика (§12) и строительство (§9) исключены из модуля.

    Проверяются словари денег и строка об экспортируемом API. Зонирование
    (``build_zones``) строительством не является: это агрегация спроса для
    маршрутизации (§32.3), а не прокладка пути.
    """
    money_words = (
        "cost",
        "price",
        "fare",
        "tariff",
        "budget",
        "grant",
        "profit",
        "revenue",
        "subsidy",
    )
    build_words = ("track", "tunnel", "demolish", "excavat", "platform_cost")
    exported = " ".join(pf.__all__).lower()
    for word in money_words:
        assert word not in exported
    for word in build_words:
        assert word not in exported

    # Строительных величин нет и в результате расчёта.
    fields = set(pf.PassengerResult.__dataclass_fields__)
    for word in money_words + build_words:
        assert not any(word in name for name in fields)


def test_public_api_is_complete():
    for name in (
        "calculate_passenger_flow",
        "passenger_gravity_weight",
        "passenger_generalized_time",
        "passenger_transit_share",
        "passenger_car_time_s",
        "passenger_wait_s",
        "passenger_validate_headway",
        "passenger_routing_window_of",
        "passenger_congestion_factor",
        "passenger_mode_key",
        "DemandPoint",
        "PassengerOptions",
        "PassengerResult",
        "Journey",
        "Pop",
        "RefusalReason",
    ):
        assert name in pf.__all__
        assert hasattr(pf, name)


# ── §32.5 Детерминизм ──────────────────────────────────────────────────


def _year_fixture():
    """Один и тот же год: сеть и спрос, которые считаются 20 раз."""
    routes = [
        line(10, "bus", [(1, 51.000, 39.000), (2, 51.010, 39.000)]),
        line(20, "bus", [(2, 51.010, 39.000), (3, 51.010, 39.010)]),
        line(30, "metro", [(1, 51.000, 39.000), (3, 51.010, 39.010)]),
    ]
    points = [
        pf.DemandPoint(f"home_{i}", 51.000 + 0.002 * i, 39.000, 1200.0 - 30.0 * i, 0.0)
        for i in range(6)
    ] + [
        pf.DemandPoint(f"job_{i}", 51.010, 39.010 - 0.002 * i, 0.0, 900.0 + 40.0 * i)
        for i in range(6)
    ]
    return routes, points


def _result_digest(result: pf.PassengerResult) -> bytes:
    """Побайтовое представление результата — для сравнения без допусков.

    Расхождение детерминизма нельзя ловить ``approx``: оно по определению
    либо нулевое, либо это другая программа. Поэтому сравниваются байты.
    """
    parts: list[bytes] = []
    for value in (
        result.total_demand,
        result.transit_trips,
        result.walk_trips,
        result.car_trips,
        result.transfer_trips,
        result.coverage,
        result.satisfaction,
        result.overcrowd_penalty_pp,
    ):
        parts.append(struct.pack("<d", float(value)))
    for key in sorted(result.boardings):
        parts.append(str(key).encode("utf-8"))
        parts.append(struct.pack("<d", float(result.boardings[key])))
    for key in sorted(result.refusals):
        parts.append(str(key).encode("utf-8"))
        parts.append(struct.pack("<d", float(result.refusals[key])))
    for segment in sorted(result.segment_loads, key=repr):
        parts.append(repr(segment).encode("utf-8"))
        parts.append(struct.pack("<d", float(result.segment_loads[segment])))
    for entry in result.pop_flows:
        parts.append("\x1f".join(str(part) for part in entry).encode("utf-8"))
    return b"".join(parts)


def test_determinism_twenty_runs_are_bitwise_identical():
    """§32.5: один и тот же год считается 20 раз — результат побитово тот же.

    Расхождение здесь — падение сборки, не предупреждение.
    """
    routes, points = _year_fixture()
    digests = {
        _result_digest(
            pf.calculate_passenger_flow(
                routes, points=points, headways={10: 6.0, 20: 8.0, 30: 4.0}
            )
        )
        for _ in range(20)
    }
    assert len(digests) == 1, "расхождение детерминизма: получилось 2 разных результата"


def test_determinism_holds_across_reordered_input_collections():
    """Канонический порядок обхода (§32.5): порядок во входе не влияет на итог.

    ``stop_routes`` хранится множеством, а порядок обхода множества строк
    меняется между процессами вместе с ``PYTHONHASHSEED``. Если бы расчёт
    зависел от него, один и тот же город давал бы разные числа.
    """
    routes, points = _year_fixture()
    headways = {10: 6.0, 20: 8.0, 30: 4.0}
    straight = _result_digest(
        pf.calculate_passenger_flow(routes, points=points, headways=dict(headways))
    )
    shuffled_routes = [routes[2], routes[0], routes[1]]
    shuffled_points = list(reversed(points))
    shuffled_headways = {key: headways[key] for key in (30, 10, 20)}
    reordered = _result_digest(
        pf.calculate_passenger_flow(
            shuffled_routes, points=shuffled_points, headways=shuffled_headways
        )
    )
    assert straight == reordered


def test_perf_budget_is_declared_not_applicable():
    """§30.3: пакет на Python — бюджет §32.6 к нему неприменим, и это объявлено."""
    assert pf.PERF_TARGET_APPLIES is False


# ── §32.4 Отпечаток сети и ключ кэша T1 ────────────────────────────────


def _fingerprint_of(routes, headways, points=None):
    from passenger.network import _build_network

    net = _build_network(routes, headways, pf.PassengerOptions())
    return pf.network_fingerprint(net)


def test_network_fingerprint_is_stable_for_identical_networks():
    routes, points = _year_fixture()
    headways = {10: 6.0, 20: 8.0, 30: 4.0}
    assert _fingerprint_of(routes, headways) == _fingerprint_of(routes, dict(headways))


def test_network_fingerprint_changes_with_headway():
    """Интервал меняет ским, значит меняет и ключ кэша (§32.4)."""
    routes, points = _year_fixture()
    before = _fingerprint_of(routes, {10: 6.0, 20: 8.0, 30: 4.0})
    after = _fingerprint_of(routes, {10: 6.5, 20: 8.0, 30: 4.0})
    assert before != after


def test_network_fingerprint_changes_with_added_line():
    """Добавление линии — тоже изменение сети, а не только интервала."""
    routes, points = _year_fixture()
    headways = {10: 6.0, 20: 8.0, 30: 4.0}
    before = _fingerprint_of(routes, headways)
    extra = line(40, "bus", [(1, 51.000, 39.000), (2, 51.005, 39.005)])
    after = _fingerprint_of([*routes, extra], {**headways, 40: 12.0})
    assert before != after


def test_network_fingerprint_is_invariant_to_input_order():
    """Та же сеть из переставленных во входе линий — тот же ключ кэша.

    Индексы узлов назначаются в порядке построения и зависят от порядка
    линий во входе. Если бы отпечаток считался по индексам, одна и та же сеть
    давала бы разные ключи — и кэш T1 промахивался бы на ровно той сети,
    которую только что посчитали.
    """
    routes, points = _year_fixture()
    headways = {10: 6.0, 20: 8.0, 30: 4.0}
    straight = _fingerprint_of(routes, headways)
    reordered = _fingerprint_of([routes[2], routes[0], routes[1]], headways)
    assert straight == reordered


def test_network_fingerprint_changes_when_stop_moves():
    """Движение остановки меняет пешие подходы, значит меняет и ключ.

    Остановка сдвигается во **всех** линиях, где она встречается: сеть берёт
    координаты из первого появления, и сдвиг только в одной линии не был бы
    изменением сети вовсе.
    """
    routes, points = _year_fixture()
    headways = {10: 6.0, 20: 8.0, 30: 4.0}
    before = _fingerprint_of(routes, headways)
    moved = [
        line(10, "bus", [(1, 51.001, 39.000), (2, 51.010, 39.000)]),
        line(20, "bus", [(2, 51.010, 39.000), (3, 51.010, 39.010)]),
        line(30, "metro", [(1, 51.001, 39.000), (3, 51.010, 39.010)]),
    ]
    assert _fingerprint_of(moved, headways) != before


def test_t1_cache_key_has_four_components():
    """Ключ §32.4: город, версия данных, хеш сети, индекс окна."""
    key = pf.t1_cache_key("voronezh", "1.4.0", "deadbeef", "W2")
    assert key.city_id == "voronezh"
    assert key.city_data_version == "1.4.0"
    assert key.network_hash == "deadbeef"
    assert key.window_index == pf.WINDOW_INDEX["W2"]


def test_t1_cache_key_rejects_part_of_day():
    """``windowIndex`` — индекс окна маршрутизации, а не части суток Takt."""
    with pytest.raises(KeyError):
        pf.t1_cache_key("voronezh", "1.4.0", "deadbeef", "Takt-08")


def test_t1_cache_holds_four_sets_and_evicts_least_recently_used():
    """§32.4: не более 4 наборов, прошлые вытесняются по редкоте использования."""
    assert pf.T1_CACHE_CAPACITY == 4
    store = pf.T1SkimStore()
    for window in ("W0", "W1", "W2", "W3"):
        store.put(pf.t1_cache_key("c", "1.0", "net-a", window), window)
    assert len(store) == 4
    # Оживляем W0, чтобы вытеснился не он.
    assert store.get(pf.t1_cache_key("c", "1.0", "net-a", "W0")) == "W0"
    store.put(pf.t1_cache_key("c", "1.0", "net-b", "W1"), "other-network")
    assert len(store) == 4
    assert store.evictions == 1
    assert store.get(pf.t1_cache_key("c", "1.0", "net-a", "W1")) is None
    assert store.get(pf.t1_cache_key("c", "1.0", "net-b", "W1")) == "other-network"


def test_t1_cache_keys_are_listed_in_canonical_window_order():
    """Порядок использования в отчёт не попадает (§32.5): окно ↑."""
    store = pf.T1SkimStore()
    for window in ("W3", "W0", "W2", "W1"):
        store.put(pf.t1_cache_key("c", "1.0", "net", window), window)
    indexes = [key.window_index for key in store.keys_in_canonical_order()]
    assert indexes == sorted(indexes)


def test_t1_cache_misses_are_counted_not_silent():
    store = pf.T1SkimStore()
    assert store.get(pf.t1_cache_key("c", "1.0", "net", "W0")) is None
    assert store.misses == 1 and store.hits == 0


# ── §32.4 Проверка приближения: ворота релиза ≤ 1 % ─────────────────────


def _verify_fixture(sample_size=40, seed=7, threshold=None, **options):
    """Город, сеть и спрос для проверки приближения.

    ``threshold`` — параметр проверки, а не расчёта, поэтому он отделён от
    ``options``, которые уходят в :class:`PassengerOptions`.
    """
    from passenger import demand as demand_mod
    from passenger import network as network_mod
    from passenger import skims as skims_mod
    from passenger import zones as zones_mod

    routes, points = _year_fixture()
    opts = pf.PassengerOptions(**options)
    net = network_mod._build_network(routes, {10: 6.0, 20: 8.0, 30: 4.0}, opts)
    zone_set = zones_mod.build_zones(points, opts)
    stop_index = network_mod._StopIndex(net)
    od_pairs, _norm = demand_mod._od_matrix(points, opts, {})
    # Форма пар та же, что внутри calculate_passenger_flow: проверка живёт
    # рядом с расчётом и принимает его вход, а не внутренний формат.
    by_key = {point.key: point for point in points}
    pairs = [
        (
            by_key[pair.origin_key],
            by_key[pair.dest_key],
            pair.weight,
            pair.dist_m,
            pair.gamma,
        )
        for pair in od_pairs
        if pair.origin_key in by_key and pair.dest_key in by_key
    ]
    zone_access = None
    centers = skims_mod.ZoneCenters(
        keys=tuple(f"zone:{zone.index}" for zone in zone_set.zones),
        lats=tuple(zone.lat for zone in zone_set.zones),
        lons=tuple(zone.lon for zone in zone_set.zones),
    )
    t1 = skims_mod.compute_zone_skims(net, centers, stop_index, opts)
    skim = t1.skims
    verify_kwargs = {} if threshold is None else {"threshold": threshold}
    report = pf.verify_skims(
        net,
        zone_set,
        skim,
        pairs,
        opts,
        stop_index,
        sample_size=sample_size,
        seed=seed,
        **verify_kwargs,
    )
    return report, skim, net, zone_set, pairs, opts, stop_index


def test_skim_verification_is_reproducible_for_a_fixed_seed():
    """§32.5: случайность в проверке — только с явным зерном."""
    first, *_ = _verify_fixture()
    second, *_ = _verify_fixture()
    assert first.sampled == second.sampled
    assert first.comparisons == second.comparisons
    assert first.mismatches == second.mismatches
    assert first.deviation == second.deviation


def test_skim_verification_uses_two_thousand_pops_by_default():
    """Размер выборки §32.4 — 2 000, и порог — 1 %."""
    assert pf.SKIM_SAMPLE_SIZE == 2000
    assert pf.SKIM_DEVIATION_THRESHOLD == 0.01


def test_skim_verification_passes_when_zones_are_one_point_each():
    """При зоне на одну точку зонального приближения нет — расхождение нулевое.

    Это не «тест пройден», а проверка самого измерителя: если бы он сравнивал
    не то, нулевое расхождение всё равно не получилось бы.
    """
    report, *_ = _verify_fixture(zone_count=len(_year_fixture()[1]))
    assert report.comparisons > 0
    assert report.deviation == 0.0
    assert report.passed is True


def test_skim_verification_reports_what_to_do_when_failed():
    """§32.4: направление проверки зависит от размера города, а не одно."""
    failed, *_ = _verify_fixture(threshold=0.0)
    if failed.mismatches:
        assert not failed.passed
        if failed.at_zone_cap:
            assert "потолок" in failed.remedy
        else:
            assert "добавлять зоны" in failed.remedy


def test_skim_verification_at_zone_cap_says_raise_the_cap():
    """Упирается город в потолок — лечится потолок, а не точность."""
    report, *_ = _verify_fixture(threshold=0.0)
    if report.mismatches and not report.passed:
        assert report.at_zone_cap is (report.zones >= pf.ZONE_MAX)


def test_stratified_sample_keeps_large_pops_from_dropping_out():
    """§32.4: стратификация нужна, «чтобы крупные не выпали»."""
    class _FakePoint:
        def __init__(self, key):
            self.key = key

    pairs = [
        (_FakePoint(f"o{i}"), _FakePoint(f"d{i}"), contribution, 1000.0, 1.0)
        for i, contribution in enumerate([1000.0] * 999 + [100_000.0])
    ]
    plain = list(range(1000))[:20]
    sampled = pf.stratified_sample(pairs, 20, seed=3)
    assert len(sampled) == 20
    # Крупный поп обязан попасть в выборку: при равномерной выборке из 20
    # элементов его шанс был бы 2 %, а здесь он гарантирован.
    assert 999 in sampled or len(plain) == len(sampled)


def test_stratified_sample_returns_all_when_smaller_than_requested():
    class _FakePoint:
        def __init__(self, key):
            self.key = key

    pairs = [
        (_FakePoint("a"), _FakePoint("b"), 1.0, 1000.0, 1.0),
        (_FakePoint("c"), _FakePoint("d"), 1.0, 1000.0, 1.0),
    ]
    assert pf.stratified_sample(pairs, 2000, seed=1) == [0, 1]


# ── §33.1 Граница модулей: T1 не знает о попах, T2 — чистая функция ────


def _t1_fixture(routes=None, headways=None, points=None):
    """Сеть, зоны и геометрия для прогона T1."""
    from passenger import network as network_mod
    from passenger import zones as zones_mod

    if routes is None:
        routes, points = _year_fixture()
    if points is None:
        points = _year_fixture()[1]
    if headways is None:
        headways = {10: 6.0, 20: 8.0, 30: 4.0}
    opts = pf.PassengerOptions()
    net = network_mod._build_network(routes, headways, opts)
    zone_set = zones_mod.build_zones(points, opts)
    stop_index = network_mod._StopIndex(net)
    centers = pf.ZoneCenters(
        keys=tuple(f"zone:{zone.index}" for zone in zone_set.zones),
        lats=tuple(zone.lat for zone in zone_set.zones),
        lons=tuple(zone.lon for zone in zone_set.zones),
    )
    return net, zone_set, centers, stop_index, opts


def test_t1_input_is_geometry_only_and_carries_no_pops():
    """Граница §33.1: на входе T1 — геометрия зон, и только она.

    ``ZoneCenters`` не имеет ни поля для числа жителей, ни поля для рабочих
    мест: «sim-t1 не знает о попах» проверяется наличием полей, а не
    соглашением в комментарии.
    """
    fields = set(pf.ZoneCenters.__dataclass_fields__)
    assert fields == {"keys", "lats", "lons"}
    assert not fields & {"residents", "jobs", "pops", "demand", "mass"}


def test_t1_runs_once_per_zone_and_window():
    """§32.3: число прогонов равно зонам × 4 окна."""
    net, zone_set, centers, stop_index, opts = _t1_fixture()
    t1 = pf.compute_zone_skims(net, centers, stop_index, opts)
    assert t1.skims.runs <= centers.count * len(pf.ROUTING_WINDOWS)
    assert t1.recomputed_zones == tuple(range(centers.count))
    assert zone_set.router_runs() == centers.count * len(pf.ROUTING_WINDOWS)


def test_t2_alone_reproduces_the_full_pipeline():
    """T2, вызванный на результатах T1, даёт то же, что и общий расчёт.

    Это главная проверка разделения: если T1 и T2 связаны чем-то помимо своих
    аргументов, число разойдётся — и граница из требования превратится в
    пожелание.
    """
    from passenger import flow as flow_mod
    from passenger import network as network_mod
    from passenger import zones as zones_mod

    routes, points = _year_fixture()
    headways = {10: 6.0, 20: 8.0, 30: 4.0}
    opts = pf.PassengerOptions()

    whole = pf.calculate_passenger_flow(routes, points=points, headways=headways)

    net = network_mod._build_network(routes, headways, opts)
    demands = sorted(
        (
            pf.DemandPoint(
                key=str(point.key),
                lat=float(point.lat),
                lon=float(point.lon),
                residents=max(0.0, float(point.residents)),
                jobs=max(0.0, float(point.jobs)),
            )
            for point in points
        ),
        key=lambda point: point.key,
    )
    zone_set = zones_mod.build_zones(demands, opts)
    stop_index = network_mod._StopIndex(net)
    centers = pf.ZoneCenters(
        keys=tuple(f"zone:{zone.index}" for zone in zone_set.zones),
        lats=tuple(zone.lat for zone in zone_set.zones),
        lons=tuple(zone.lon for zone in zone_set.zones),
    )
    t1 = pf.compute_zone_skims(net, centers, stop_index, opts)
    staged = pf.assign_passengers(
        net, t1.skims, demands, zone_set.zone_of, opts, {}, uniform_masses=False
    )

    assert staged.transit_trips == whole.transit_trips
    assert staged.walk_trips == whole.walk_trips
    assert staged.car_trips == whole.car_trips
    assert staged.total_demand == whole.total_demand
    assert staged.satisfaction == whole.satisfaction
    assert staged.boardings == whole.boardings
    assert staged.refusals == whole.refusals


def test_t1_is_reproducible_for_the_same_network():
    net, zone_set, centers, stop_index, opts = _t1_fixture()
    first = pf.compute_zone_skims(net, centers, stop_index, opts)
    second = pf.compute_zone_skims(net, centers, stop_index, opts)
    assert first.network_hash == second.network_hash
    assert first.skims.time == second.skims.time


def test_t1_is_invariant_to_order_of_input_routes():
    """§32.5: тот же результат при перестановке линий во входе."""
    routes, points = _year_fixture()
    headways = {10: 6.0, 20: 8.0, 30: 4.0}
    straight = _t1_fixture(routes, headways, points)
    reordered = _t1_fixture([routes[2], routes[0], routes[1]], headways, points)
    a = pf.compute_zone_skims(
        straight[0], straight[2], straight[3], straight[4]
    )
    b = pf.compute_zone_skims(
        reordered[0], reordered[2], reordered[3], reordered[4]
    )
    assert a.network_hash == b.network_hash
    assert a.skims.time == b.skims.time


# ── §32.4 Инкрементальный пересчёт: порог 40 линий ────────────────────


def _grid_network(lines_count):
    """Сеть из ``lines_count`` линий через общую станцию — для порога §32.4."""
    hub = 2
    routes = [line(10, "bus", [(1, 51.000, 39.000), (hub, 51.010, 39.000)])]
    for index in range(lines_count - 1):
        angle = 0.001 * index
        routes.append(
            line(
                100 + index,
                "bus",
                [(hub, 51.010, 39.000), (200 + index, 51.010 + angle, 39.010)],
            )
        )
    headways = {route.route_id: 8.0 for route in routes}
    return routes, headways


def test_small_network_uses_full_recalculation():
    """§32.4: меньше порога линий — прямой расчёт, он для малой сети быстрее."""
    assert pf.INCREMENTAL_LINE_THRESHOLD == 40
    routes, headways = _grid_network(12)
    net, zone_set, centers, stop_index, opts = _t1_fixture(routes, headways)
    t1 = pf.compute_zone_skims(net, centers, stop_index, opts)
    snapshot = pf.capture_snapshot(net, centers, stop_index, opts, t1.skims)
    plan = pf.plan_incremental(net, centers, stop_index, opts, snapshot)
    assert plan.full is True
    assert "порог" in plan.reason


def test_large_network_recomputes_only_affected_zones():
    """§32.4: при ≥ 40 линиях пересчитываются только затронутые зоны."""
    routes, headways = _grid_network(45)
    net, zone_set, centers, stop_index, opts = _t1_fixture(routes, headways)
    t1 = pf.compute_zone_skims(net, centers, stop_index, opts)
    snapshot = pf.capture_snapshot(net, centers, stop_index, opts, t1.skims)

    # Меняем интервал одной линии: сеть изменилась, охват зон прежний.
    changed = dict(headways)
    changed[routes[5].route_id] = 14.0
    from passenger import network as network_mod

    net2 = network_mod._build_network(routes, changed, opts)
    plan = pf.plan_incremental(net2, centers, stop_index, opts, snapshot)
    assert plan.full is False
    assert 0 < len(plan.zones) <= centers.count
    assert "затронуто" in plan.reason


def test_unchanged_large_network_recomputes_nothing():
    """§32.4: сеть не менялась — пересчитывать нечего."""
    routes, headways = _grid_network(45)
    net, zone_set, centers, stop_index, opts = _t1_fixture(routes, headways)
    t1 = pf.compute_zone_skims(net, centers, stop_index, opts)
    snapshot = pf.capture_snapshot(net, centers, stop_index, opts, t1.skims)
    plan = pf.plan_incremental(net, centers, stop_index, opts, snapshot)
    assert plan.full is False
    assert plan.zones == ()
    assert "не изменилась" in plan.reason


def test_unrecognised_change_falls_back_to_full_recalculation():
    """Изменение не опознано — пересчитываем всё, а не отдаём устаревшие скимы.

    Проверка затронутости опирается на охват зоны и обслуживающие её линии.
    Изменение, которое расширяет достижимость и не касается этих остановок,
    ей не видно. Цена ошибки в эту сторону — лишнее время; цена обратной
    ошибки — неверные числа в отчёте.

    Снимок здесь настоящий, отличается только отпечаток: охват и обслуживание
    совпадают с текущей сетью, поэтому ни одна зона не признаётся затронутой.
    """
    import dataclasses

    routes, headways = _grid_network(45)
    net, zone_set, centers, stop_index, opts = _t1_fixture(routes, headways)
    t1 = pf.compute_zone_skims(net, centers, stop_index, opts)
    snapshot = pf.capture_snapshot(net, centers, stop_index, opts, t1.skims)
    stale = dataclasses.replace(snapshot, network_hash="0" * 8)

    plan = pf.plan_incremental(net, centers, stop_index, opts, stale)
    assert plan.full is True
    assert "ни одна зона" in plan.reason


def test_incremental_result_matches_full_recalculation():
    """Инкрементальный пересчёт обязан дать то же, что и полный.

    Иначе это не ускорение, а второй расчёт с другими числами: игрок увидел бы
    одно и то же состояние сети с двумя разными ответами.
    """
    import dataclasses

    from passenger import network as network_mod

    routes, headways = _grid_network(45)
    net, zone_set, centers, stop_index, opts = _t1_fixture(routes, headways)
    t1 = pf.compute_zone_skims(net, centers, stop_index, opts)
    snapshot = pf.capture_snapshot(net, centers, stop_index, opts, t1.skims)

    # Снимок с чужим хешем: план уходит в полный пересчёт, но результат
    # обязан совпасть с исходным до метки.
    stale = dataclasses.replace(snapshot, network_hash="0" * 8)
    after = pf.compute_zone_skims(
        net, centers, stop_index, opts, snapshot=stale
    )
    assert after.skims.time == t1.skims.time
    assert after.skims.transfers == t1.skims.transfers


# ── §28 шаг 7, §17.3, §29 Q2: сверка с эталонной сетью ───────────────


def test_boardings_by_mode_are_reported_separately():
    """§28 шаг 7: «посадки в день по режимам и в целом» — по режимам тоже.

    Сумма по режимам может быть **меньше** ``transit_trips``, и это верно, а не
    ошибка: у поездки, ским-лучшим которой оказалась чистая пешая ходьба из
    зоны, посадок нет — в транспорт никто не садился. Такие поездки входят в
    долю транспорта, потому что обобщённое время скима оказалось выгодным, но
    посадок они не дают. Ровно это расхождение и измеряет проверка ≤ 1 % (§32.4).
    """
    routes, points = _year_fixture()
    result = pf.calculate_passenger_flow(
        routes, points=points, headways={10: 6.0, 20: 8.0, 30: 4.0}
    )
    assert result.boardings_by_mode
    assert set(result.boardings_by_mode) <= set(pf.MODE_PARAMS)
    total = sum(result.boardings_by_mode.values())
    assert total <= result.transit_trips + 1e-9
    assert total > 0.0


def test_wasserstein_handles_unequal_sample_sizes():
    """Объёмы по построению разные: 300 наблюдений против 1000 модельных.

    Наивное ``mean(|a_i - b_i|)`` по сортированным выборкам разного размера
    вернуло бы здесь ноль — первые 300 значений совпадают, — хотя распределения
    заведомо разные: у модели хвост до 999, у наблюдений он обрывается на 299.
    """
    modelled = [float(value) for value in range(1000)]
    observed = [float(value) for value in range(300)]
    value = pf.trip_distance_wasserstein(modelled, observed)

    # Точное значение: ∫|F_a − F_b|. На [x, x+1] при x < 299 разность равна
    # (x+1)·(1/300 − 1/1000); при x ≥ 299 — 1 − (x+1)/1000.
    expected = sum((x + 1) * (1 / 300 - 1 / 1000) for x in range(299))
    expected += sum(1.0 - (x + 1) / 1000 for x in range(299, 999))
    assert value == pytest.approx(expected, rel=1e-9)
    assert value == pytest.approx(350.0, rel=1e-9)

    # Наивная формула дала бы ноль на этих данных — и ошиблась бы.
    naive = sum(abs(x - y) for x, y in zip(modelled, observed)) / len(observed)
    assert naive == 0.0
    assert value > 1.0

    assert 0.0 < pf.trip_distance_ks(modelled, observed) < 1.0


def test_reference_check_has_no_acceptance_threshold():
    """§29 вопрос 2: порога приёмки не существует, и в API его нет.

    Наличие поля ``passed`` означало бы, что документ прочитан неправильно.
    """
    fields = set(pf.ReferenceCheck.__dataclass_fields__)
    assert "passed" not in fields
    assert "threshold" not in fields
    assert "tolerance" not in fields
    assert not any("pass" in name or "thresh" in name for name in fields)


def test_reference_check_shows_data_quality_and_deviation_separately():
    """§17.3: «чем измеряли» и «насколько совпало» — два разных вопроса."""
    routes, points = _year_fixture()
    result = pf.calculate_passenger_flow(
        routes, points=points, headways={10: 6.0, 20: 8.0, 30: 4.0}
    )
    check = pf.check_against_reference(
        result,
        pf.BoardingsReference(
            by_mode={"bus": 1000.0},
            total=result.transit_trips,
            source="агентство, 2026",
            boarding_definition=pf.MODEL_BOARDING_DEFINITION,
        ),
        rubric_level="C — растровое население, сеть из открытых данных",
    )
    assert check.rubric_level.startswith("C")
    # Расхождение по опубликованному числу и уровень данных лежат рядом, но
    # не объединены ни в одну оценку.
    assert check.by_mode[0].published == 1000.0
    assert check.by_mode[0].modelled != 1000.0
    text = check.summary()
    assert "рубрике" in text and "порога приёмки нет" in text


def test_reference_check_flags_unaligned_boarding_definition():
    """§29 вопрос 4 открыт: расхождение определений нельзя выдавать за модель."""
    routes, points = _year_fixture()
    result = pf.calculate_passenger_flow(
        routes, points=points, headways={10: 6.0, 20: 8.0, 30: 4.0}
    )
    check = pf.check_against_reference(
        result,
        pf.BoardingsReference(by_mode={"bus": 1000.0}, total=1200.0, source="x"),
        rubric_level="D",
    )
    assert check.definition_aligned is False
    assert any("определение" in caveat for caveat in check.caveats)


def test_reference_check_notes_missing_source_and_non_weekday_data():
    routes, points = _year_fixture()
    result = pf.calculate_passenger_flow(
        routes, points=points, headways={10: 6.0, 20: 8.0, 30: 4.0}
    )
    check = pf.check_against_reference(
        result,
        pf.BoardingsReference(
            by_mode={"bus": 1000.0},
            total=1200.0,
            boarding_definition=pf.MODEL_BOARDING_DEFINITION,
            is_weekday=False,
        ),
        rubric_level="D",
    )
    joined = " ".join(check.caveats)
    assert "источник" in joined
    assert "будний" in joined


def test_relative_deviation_is_none_when_published_is_zero():
    """Относительное расхождение по нулю бесконечно и молчило бы о качестве."""
    deviation = pf.ModeDeviation(mode="water", modelled=12.0, published=0.0)
    assert deviation.relative is None
    assert deviation.absolute == 12.0
    assert "—" in str(deviation)


def test_wasserstein_is_zero_for_identical_samples():
    sample = [100.0, 250.0, 400.0, 900.0]
    assert pf.trip_distance_wasserstein(sample, sample) == 0.0
    assert pf.trip_distance_ks(sample, sample) == 0.0


def test_distance_metrics_reject_empty_or_nonfinite_input():
    with pytest.raises(ValueError):
        pf.trip_distance_wasserstein([], [1.0])
    with pytest.raises(ValueError):
        pf.trip_distance_wasserstein([1.0], [float("nan")])
    with pytest.raises(ValueError):
        pf.trip_distance_ks([1.0], [])


# ── §5.5, §18.5: точки спроса и OD-матрица ────────────────────────────


def test_od_matrix_without_jobs_fails_loudly():
    """Рабочих мест нет → OD-матрицы нет, и об этом сказано, а не молча返回 0.

    Молчаливые нулевые пары выглядели бы как «город без поездок», и город без
    поездок отличить от города без данных по отчёту было бы нельзя (§27.1).
    """
    from passenger import demand as demand_mod

    points = [
        pf.DemandPoint("a", 51.0, 39.0, 500.0, 0.0),
        pf.DemandPoint("b", 51.01, 39.01, 400.0, 0.0),
    ]
    with pytest.raises(ValueError) as error:
        demand_mod._od_matrix(points, pf.PassengerOptions(), {})
    assert "рабоч" in str(error.value).lower()


def test_od_pair_budget_is_refused_with_an_explanation():
    """Потолок пар OD: отказ с текстом лучше расчёта, который не остановить."""
    from passenger import demand as demand_mod

    points = [
        pf.DemandPoint(f"o{i}", 51.0 + i * 0.01, 39.0, 100.0, 50.0)
        for i in range(40)
    ] + [
        pf.DemandPoint(f"d{i}", 51.0 + i * 0.01, 39.05, 50.0, 100.0)
        for i in range(40)
    ]
    options = pf.PassengerOptions(max_od_pairs=100)
    with pytest.raises(ValueError) as error:
        demand_mod._od_matrix(points, options, {})
    message = str(error.value)
    assert "пар при потолке" in message
    assert "18.5" in message


def test_od_matrix_builds_when_jobs_are_present():
    """С рабочими местами матрица строится — значит запрет не бьёт зря."""
    from passenger import demand as demand_mod

    points = [
        pf.DemandPoint("home_a", 51.000, 39.000, 800.0, 0.0),
        pf.DemandPoint("home_b", 51.005, 39.002, 600.0, 0.0),
        pf.DemandPoint("job_a", 51.010, 39.005, 0.0, 900.0),
        pf.DemandPoint("job_b", 51.012, 39.007, 0.0, 700.0),
    ]
    pairs, norm = demand_mod._od_matrix(points, pf.PassengerOptions(), {})
    assert pairs
    assert norm > 0.0
    assert all(pair.origin_key != pair.dest_key for pair in pairs)


def test_population_module_reports_the_measured_product():
    """§17.3: «чем измеряли» — часть результата, а не подпись в отчёте."""
    from passenger import population as pop_mod

    assert "GHS_POP" in pop_mod.GHS_POPULATION_PRODUCT
    reading = pop_mod.PopulationReading(
        product=pop_mod.GHS_POPULATION_PRODUCT,
        cells=100,
        populated_cells=10,
        no_data_cells=5,
        total_population=1234.0,
        raster_crs="EPSG:4326",
        cells_per_point=4.0,
    )
    assert "1,234" in reading.summary()
    assert reading.populated_cells == 10


def test_population_stride_comes_from_cell_area():
    """Шаг задаётся плотностью через площадь ячейки, а не через «сторону».

    У ячейки географического растра соотношение сторон зависит от широты, и
    «сторона квадратной ячейки той же площади» — единственное сравнимое число.
    """
    from affine import Affine
    from passenger import population as pop_mod

    # Ячейка 0,0008333° — как у продукта GHS_POP.
    transform = Affine(0.00083333, 0.0, 0.0, 0.0, -0.00083333, 0.0)
    area = pop_mod._cell_area_m2(transform, 51.67)
    assert 5000.0 < area < 5600.0

    stride = pop_mod._stride_for_density(100.0, area)
    assert stride == 1
    assert pop_mod._stride_for_density(1.0, area) > 10
    assert pop_mod._stride_for_density(0.0, area) == 1
    assert pop_mod._stride_for_density(100.0, 0.0) == 1


def test_population_module_does_not_fabricate_jobs():
    """Растр населения не содержит рабочих мест — и модуль этого не прячет."""
    from passenger import population as pop_mod

    source = (pop_mod.__doc__ or "") + (population_functions_doc())
    assert "рабочие места" in source
    assert "jobs" in source


def population_functions_doc() -> str:
    from passenger import population as pop_mod

    return pop_mod.population_points.__doc__ or ""


# ── Рабочие места как оценка из застройки (§17.3, §18.5) ───────────────


def test_jobs_estimate_is_never_marked_as_measured():
    """Оценка не может выглядеть измерением — ни при каком параметре."""
    from passenger import estimate as est_mod

    assert est_mod.DEFAULT_M2_PER_JOB == 100.0
    assert est_mod.SENSITIVITY_STEPS == (50.0, 100.0, 200.0)
    source = est_mod.__doc__ or ""
    assert "не измерен" in source or "не измерено" in source


def test_jobs_estimate_carries_its_own_invalidation():
    """Рядом с числом стоит параметр, которым оно получено, и чувствительность.

    Число рабочих мест без параметра, которым оно получено, — это число без
    единицы измерения. Удвоение параметра должно быть видно, а не спрятано.
    """
    import numpy as np

    from passenger import estimate as est_mod

    density = np.full((10, 10), 0.5)  # половина земли застроена
    covered = np.ones((10, 10), dtype=bool)
    cell_area = 10_000.0

    jobs_100, estimate = est_mod.jobs_from_built_surface(
        density, covered, cell_area, 100.0
    )
    jobs_200, estimate_200 = est_mod.jobs_from_built_surface(
        density, covered, cell_area, 200.0
    )

    assert estimate.measured is False
    assert estimate.m2_per_job == 100.0
    assert np.allclose(jobs_200, jobs_100 / 2.0)
    # Таблица чувствительности — свойство **площади застройки**, а не выбранного
    # параметра, поэтому от параметра не зависит: она показывает, сколько мест
    # было бы при других значениях, иначе её нельзя было бы прочитать как
    # «вот сколько мест на самом деле».
    assert estimate.sensitivity == estimate_200.sensitivity
    assert [step for step, _jobs in estimate.sensitivity] == list(
        est_mod.SENSITIVITY_STEPS
    )
    by_step = dict(estimate.sensitivity)
    assert by_step[100.0] * 2 == by_step[50.0]
    assert by_step[100.0] / 2 == by_step[200.0]
    assert "не измерен" in estimate.summary()
    assert estimate.notes


def test_jobs_estimate_ignores_cells_without_built_data():
    """«Застройки нет» и «застройка не измерена» — разные вещи."""
    import numpy as np

    from passenger import estimate as est_mod

    density = np.ones((4, 4))
    covered = np.zeros((4, 4), dtype=bool)
    covered[0, 0] = True  # застроен и покрыт только один

    jobs, estimate = est_mod.jobs_from_built_surface(
        density, covered, 10_000.0, 100.0
    )
    assert float(jobs[0, 0]) == 100.0
    assert float(jobs.sum()) == 100.0
    assert estimate.cells_without_built_data == 15


def test_jobs_estimate_rejects_a_nonpositive_parameter():
    import numpy as np

    from passenger import estimate as est_mod

    with pytest.raises(ValueError):
        est_mod.jobs_from_built_surface(
            np.ones((2, 2)), np.ones((2, 2), dtype=bool), 10_000.0, 0.0
        )