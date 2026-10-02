"""Назначение пассажиров на сеть и сборка результата (§32.2, T2).

Здесь соединяются спрос, скимы и конкуренты-автомобиль с пешим
вариантом; считаются показатели и причины отказа.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from .demand import _od_matrix
from .time_model import WINDOW_FLOWS
from .geo import _haversine_m
from .params import (
    DemandPoint,
    FIXED_POINT_DECIMALS,
    MODE_PARAMS,
    PassengerOptions,
    PassengerResult,
    ROUTING_WINDOWS,
    RefusalReason,
    WINDOW_CONGESTION,
    W_RIDE,
    W_WALK,
)

from .network import (
    _Network,
    _StopIndex,
    _access_states,
    _build_network,
)

from .skims import (
    ZoneCenters,
    ZoneSkims,
    _ZoneOutcome,
    _mode_shares,
    _zone_outcome,
    compute_zone_skims,
)

from .zones import (
    build_zones,
)


def _collect_demands(
    net: _Network,
    masses: dict[str, tuple[float, float]] | None,
    options: PassengerOptions,
) -> tuple[list[DemandPoint], bool]:
    """Строит точки спроса; без масс — равномерные (флаг в результате).

    По умолчанию каждая остановка даёт точку спроса (§5.2: точки спроса —
    дискретные узлы с населением и рабочими местами, а не обязательно
    остановки). Ключ узла — ``id:<id>`` при наличии id, иначе
    ``geo:<lat>,<lon>``.

    Если ``masses`` задана, она полная: у ключа, которого в ней нет, масса
    **нулевая**. Частичное задание молча подставило бы население по
    умолчанию и создало бы потоки, которых игрок не заказывал.
    """
    uniform = masses is None
    demands: list[DemandPoint] = []
    for idx, key in enumerate(net.keys):
        if masses is not None:
            residents, jobs = masses.get(key, (0.0, 0.0))
        else:
            residents = float(options.default_population)
            jobs = float(options.default_population)
        demands.append(
            DemandPoint(
                key=key,
                lat=net.lats[idx],
                lon=net.lons[idx],
                residents=max(0.0, float(residents)),
                jobs=max(0.0, float(jobs)),
            )
        )
    return demands, uniform

def calculate_passenger_flow(
    routes: Any,
    *,
    masses: dict[str, tuple[float, float]] | None = None,
    points: list[DemandPoint] | None = None,
    headways: dict[Any, float] | None = None,
    categories: dict[str, str] | None = None,
    options: PassengerOptions | None = None,
) -> PassengerResult:
    """Считает пассажиропоток сети за средний будний день.

    :param routes: маршруты (объекты с ``route_id``/``route_type``/
        ``directions``; направления — с ``stops``). Совместимы с
        ``wikiroutes.models.RouteData``.
    :param masses: ``ключ → (жители, рабочие_места)`` для точек, ключ которых
        совпадает с ключом остановки (``id:<id>`` либо ``geo:<lat>,<lon>``).
    :param points: явные точки спроса с собственными координатами (§5.2).
        Нужны, чтобы точки спроса не совпадали с остановками: только тогда
        «в пешем охвате» (§13.3) может быть меньше единицы. Приоритетнее
        ``masses``.
    :param headways: ``route_id → интервал_мин``; ``0``/``None`` — линия
        стоит и в сеть не входит. Без них — дефолты режимов (§8.5).
    :param categories: ``ключ_назначения → код_категории`` (``AIR``,
        ``SCH``, …) для показателя γ (§5.5).
    :param options: настройки расчёта.
    """
    opts = options or PassengerOptions()
    categories = categories or {}
    net = _build_network(routes, headways, opts)

    def _empty(uniform: bool = True) -> PassengerResult:
        return PassengerResult(
            total_demand=0.0,
            transit_trips=0.0,
            walk_trips=0.0,
            car_trips=0.0,
            transfer_trips=0.0,
            coverage=0.0,
            satisfaction=0.0,
            overcrowd_penalty_pp=0.0,
            boardings={},
            boardings_by_mode={},
            segment_loads={},
            segment_load_factor={},
refusals={reason.value: 0.0 for reason in RefusalReason},
            uniform_masses=uniform,
            pop_flows=(),
        )

    if not net.keys:
        return _empty()

    if points is not None:
        demands = [
            DemandPoint(
                key=str(point.key),
                lat=float(point.lat),
                lon=float(point.lon),
                residents=max(0.0, float(point.residents)),
                jobs=max(0.0, float(point.jobs)),
            )
            for point in points
        ]
        uniform = not masses
    else:
        demands, uniform = _collect_demands(net, masses, opts)

    # Канонический порядок обхода спроса (§32.5). Задаётся один раз, здесь:
    # от него зависят и OD-матрица с её нормировкой, и зонирование, и само
    # суммирование потоков. Порядок во входе — от файла города, из разных
    # источников и в разном порядке — на результат влиять не должен.
    # Сортировка полная (ключ, потом координаты и массы): одинаковые ключи не
    # должны оставлять порядок неопределённым.
    demands.sort(
        key=lambda point: (
            point.key,
            point.lat,
            point.lon,
            point.residents,
            point.jobs,
        )
    )

    # ── Шаг 1: OD-матрица (§5.5) ──
    # Матрица не зависит от сети игрока, поэтому считается один раз в
    # инструментарии и хранится в пакете города (`demand_data`, §18.5).
    # Здесь она либо приходит готовой, либо строится на месте для удобства.
    origins = [d for d in demands if d.residents > 0.0]
    total_production = sum(d.residents for d in origins) * float(opts.trips_per_person)

    od_pairs, norm = _od_matrix(demands, opts, categories)
    by_key = {str(point.key): point for point in demands}
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
    # Канонический порядок накопления (§32.5): пары идут по ключам, а не в
    # порядке построения. Порядок сложения плавающих чисел влияет на младшие
    # разряды, и без этой сортировки перестановка точек в городе меняла
    # transit_trips на единицу в последнем знаке.
    pairs.sort(key=lambda item: (item[0].key, item[1].key))

    if norm <= 0.0 or total_production <= 0.0:
        return _empty(uniform)

    # ── Шаг 2: зоны (§20.2, §32.3) ──
    # Начала агрегируются в зоны: один прогон маршрутизатора обслуживает всех
    # пассажиров зоны. Число прогонов определяется числом зон, а не числом
    # точек спроса (§30.1) — это и есть требование к производительности.
    zone_set = build_zones(demands, opts)
    if zone_set.count == 0:
        return _empty(uniform)

    stop_index = _StopIndex(net)

    # ── Шаг 2a: T1 — скимы по геометрии зон (§33.1) ──
    # T1 получает только центры зон и ничего не знает о попах. Граница
    # «sim-t1 не знает о попах» держится структурой данных, а не соглашением:
    # на входе геометрия, на выходе таблица «зона → зона».
    centers = ZoneCenters(
        keys=tuple(f"zone:{zone.index}" for zone in zone_set.zones),
        lats=tuple(zone.lat for zone in zone_set.zones),
        lons=tuple(zone.lon for zone in zone_set.zones),
    )
    t1 = compute_zone_skims(net, centers, stop_index, opts)

    # ── Шаг 2b: T2 — попы по парам зон, O(1) на пару (§32.3, шаг 4) ──
    return assign_passengers(
        net, t1.skims, demands, zone_set.zone_of, opts, categories,
        uniform_masses=uniform,
    )


def assign_passengers(
    net: _Network,
    skims: ZoneSkims,
    demands: list[DemandPoint],
    zone_of: dict[str, int],
    options: PassengerOptions,
    categories: dict[str, str] | None = None,
    *,
    uniform_masses: bool = True,
) -> PassengerResult:
    """T2: назначение пассажиров по парам зон — чистая функция (§33.1).

    Расчёт — чистые функции от входов (§33.1): та же сеть и тот же спрос дают
    тот же результат, а состояние между вызовами не переносится. Это и делает
    возможными и детерминизм (§32.5), и пересчёт в воркере, и проверку
    качества скима (§32.4).

    :param skims: результат T1 — таблица «зона → зона» по окнам.
    :param zone_of: отображение «ключ точки спроса → индекс зоны». Это знание
        о спросе, поэтому оно на входе T2, а не внутри T1.
    :param uniform_masses: были ли массы точек одинаковыми по умолчанию —
        признак «данных о населении не было», а не расхождение в расчёте.
    """
    opts = options
    categories = categories or {}
    stop_index = _StopIndex(net)

    origins = [point for point in demands if point.residents > 0.0]
    total_production = sum(point.residents for point in origins) * float(
        opts.trips_per_person
    )

    od_pairs, norm = _od_matrix(demands, opts, categories)
    by_key = {str(point.key): point for point in demands}
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
    # Канонический порядок накопления (§32.5): пары идут по ключам, а не в
    # порядке построения. Порядок сложения плавающих чисел влияет на младшие
    # разряды, и без этой сортировки перестановка точек в городе меняла
    # transit_trips на единицу в последнем знаке.
    pairs.sort(key=lambda item: (item[0].key, item[1].key))

    # Число зон берётся из отображения «точка → зона»: T2 работает в
    # пространстве зон, и всё, что ему нужно знать о зонировании, — это
    # отображение и его размер. Само зонирование — забота `city` (§33.1).
    zone_count = (max(zone_of.values()) + 1) if zone_of else 0

    if norm <= 0.0 or total_production <= 0.0:
        return PassengerResult(
            total_demand=0.0,
            transit_trips=0.0,
            walk_trips=0.0,
            car_trips=0.0,
            transfer_trips=0.0,
            coverage=0.0,
            satisfaction=0.0,
            overcrowd_penalty_pp=0.0,
            boardings={},
            boardings_by_mode={},
            segment_loads={},
            segment_load_factor={},
            refusals={reason.value: 0.0 for reason in RefusalReason},
            uniform_masses=uniform_masses,
            pop_flows=(),
        )

    boardings: dict[str, float] = {}
    #: Посадки в день по режимам (§28 шаг 7). Режим поездки — режим первой
    #: ветки пути: пересадка с автобуса на метро остаётся поездкой, начавшейся
    #: на автобусе, иначе один пересадной автобусный рейс считался бы метро.
    boardings_by_mode: dict[str, float] = {}
    segment_loads: dict[tuple[Any, int, int], float] = {}
    refusals: dict[str, float] = {reason.value: 0.0 for reason in RefusalReason}
    pop_flows: list[tuple[str, str, float, float, float]] = []
    #: Ежедневный поток по сегменту **в окне**: ёмкость сегмента зависит от
    #: окна, поэтому и нагрузку нельзя суммировать без его указания.
    segment_load_window_loads: dict[tuple[Any, int, int, str], float] = {}
    transit = walk_trips = car = transfer_trips = 0.0
    covered = offered = well = well_no_cap = 0.0

    tortuosity = float(opts.tortuosity)
    walk_speed = max(0.1, float(opts.walk_speed_mps))
    max_walk_s = float(opts.max_walk_to_stop_s)
    beta = float(opts.beta_per_s)
    car_speed_mps = max(1.0, float(opts.car_speed_kmh) * 1000.0 / 3600.0)
    short_threshold = float(opts.short_car_threshold_m)
    short_penalty = float(opts.short_car_penalty_s)
    peak_share = float(opts.peak_hour_share)

    # Покрытие по зонам: точка спроса покрыта, если в пешем радиусе её зоны
    # есть остановка (§13.3 — «где сеть», а не «хороша ли она»).
    # Покрытие — свойство точки, а не пары, поэтому оно считается один раз
    # на точку и переиспользуется для всех её пар.
    covered_keys: dict[str, bool] = {}

    def is_covered(point: DemandPoint) -> bool:
        key = str(point.key)
        cached = covered_keys.get(key)
        if cached is not None:
            return cached
        # Проверка чисто координатная: точка спроса и остановка — разные
        # сущности (§5.2), их ключи не обязаны совпадать. Требовать
        # совпадения ключей значило бы объявлять весь спрос вне охвата
        # всякий раз, когда точки спроса пришли из растра населения.
        found = any(
            item[1] >= 0
            for item in _access_states(net, stop_index, point, opts)
        )
        covered_keys[key] = found
        return found

    # Часовая провозная способность сегмента в окне (§8.3).
    segment_capacity: dict[tuple[Any, int, int, str], float] = {}
    for route_id, dir_idx, order_nodes in net.directions:
        schedule = net.route_schedule.get(route_id)
        mode = net.route_mode.get(route_id, "bus")
        capacity = float(MODE_PARAMS[mode]["vehicle_capacity"])
        for code, _start, _end in ROUTING_WINDOWS:
            headway = schedule.minutes_for(code) if schedule else None
            per_hour = capacity * (60.0 / headway) if headway else 0.0
            for seg_idx in range(len(order_nodes) - 1):
                segment_capacity[(route_id, dir_idx, seg_idx, code)] = per_hour

    # ── Шаг 3: T2 ──
    # Ключевое следствие зональной агрегации (§32.3): всё, что зависит только
    # от пары зон и окна, одинаково для **всех** пассажиров этой пары. Пар
    # «начало — конец» — около миллиона, а пар зон — не более 250×250. Поэтому
    # доля транспорта, качество, потеря и её причины считаются один раз на
    # пару зон и окно, а по парам OD накапливается только умножением на поток.
    # Без этого расчёт стоил бы в десятки раз дороже при том же результате.
    outcomes: dict[
        tuple[int, int, str], "_ZoneOutcome"
    ] = {}

    for origin_zone in range(zone_count):
        for dest_zone in range(zone_count):
            for code, _start_w, _end_w in ROUTING_WINDOWS:
                outcome = _zone_outcome(
                    skims, net, segment_capacity, opts,
                    origin_zone, dest_zone, code,
                )
                if outcome is not None:
                    outcomes[(origin_zone, dest_zone, code)] = outcome

    for origin, dest, contribution, dist_m, gamma in pairs:
        flow = total_production * contribution / norm
        dist_eff = dist_m * tortuosity
        pop_flows.append((str(origin.key), str(dest.key), dist_eff, gamma, flow))

        origin_zone = zone_of.get(str(origin.key))
        dest_zone = zone_of.get(str(dest.key))
        if origin_zone is None or dest_zone is None:
            continue

        covered_origin = is_covered(origin)
        covered_dest = is_covered(dest)
        if covered_origin or covered_dest:
            covered += flow

        # Автомобиль зависит от расстояния пары, а не от зон, поэтому
        # считается на паре OD; транспортный вариант — из таблицы исходов.
        car_free = dist_eff * tortuosity / car_speed_mps
        car_time = car_free * WINDOW_CONGESTION[opts.peak_hour_window]
        if dist_m < short_threshold:
            car_time += short_penalty
        car_g = car_time * W_RIDE
        walk_s = dist_eff / walk_speed
        walk_g = walk_s * W_WALK if covered_dest and walk_s <= max_walk_s else None

        for code, share in WINDOW_FLOWS["home_to_work"].items():
            window_flow = flow * share
            if window_flow < 0.5:
                continue
            # Признак переполнения считается в ветке ниже, когда известен
            # поток. Здесь он обнуляется явно, чтобы потеря причин ниже не
            # зависела от того, начислялся ли перевоз.
            overcrowded = False
            outcome = outcomes.get((origin_zone, dest_zone, code))
            if outcome is None:
                # Нет пути или точка вне охвата: разбираем по причинам.
                if not covered_origin and not covered_dest:
                    refusals[RefusalReason.FAR_FROM_STOP.value] = (
                        refusals.get(RefusalReason.FAR_FROM_STOP.value, 0.0)
                        + window_flow
                    )
                else:
                    refusals[RefusalReason.NO_PATH.value] = (
                        refusals.get(RefusalReason.NO_PATH.value, 0.0) + window_flow
                    )
                transit += 0.0
                car += window_flow * (0.0 if walk_g is not None else 1.0)
                walk_trips += window_flow * (1.0 if walk_g is not None else 0.0)
                continue

            # Доли режимов: ским даёт время транспорта, автомобиль и пеший
            # конкурент считаются на паре OD (§6.5).
            share_tr, share_walk, share_car = _mode_shares(
                outcome.gen_time, car_g, walk_g, beta
            )
            transit_flow = window_flow * share_tr
            transit += transit_flow
            walk_trips += window_flow * share_walk
            car += window_flow * share_car
            if outcome.transfers > 0:
                transfer_trips += transit_flow

            if share_tr > 0.0:
                offered += window_flow
                # Переполнение: пиковый поток против часовой провозной
                # способности пары зон в этом окне (§6.7, §8.3).
                peak_flow = transit_flow * peak_share
                capacity = outcome.min_capacity
                overcrowded = (
                    capacity > 0.0 and peak_flow > capacity
                )
                cap_factor = (
                    min(1.0, capacity / peak_flow)
                    if overcrowded and peak_flow > 0.0
                    else 1.0
                )
                well_no_cap += transit_flow * outcome.quality
                well += transit_flow * outcome.quality * cap_factor
                # Режим поездки — первая ветка пути (§28 шаг 7).
                if outcome.segments:
                    first_route = outcome.segments[0][0]
                    mode = net.route_mode.get(first_route, "bus")
                    boardings_by_mode[mode] = (
                        boardings_by_mode.get(mode, 0.0) + transit_flow
                    )
                for segment in outcome.segments:
                    segment_loads[segment] = (
                        segment_loads.get(segment, 0.0) + transit_flow
                    )
                    windowed = (*segment, code)
                    segment_load_window_loads[windowed] = (
                        segment_load_window_loads.get(windowed, 0.0) + transit_flow
                    )
                for stop_key in outcome.board_stops:
                    boardings[stop_key] = (
                        boardings.get(stop_key, 0.0) + transit_flow
                    )

            # Потеря делится между причинами пропорционально вкладу (§6.6).
            lost = window_flow - transit_flow
            if lost > 0.0 and outcome.causes:
                causes = outcome.causes
                # «Переполнение» добавляется здесь, а не в T1, потому что
                # только здесь известен поток. Причина появляется ровно тогда,
                # когда пиковый поток действительно превысил ёмкость, — иначе
                # список уводил бы игрока добавлять вместимость в пустые вагоны.
                if overcrowded:
                    causes = {
                        **causes,
                        RefusalReason.OVERCROWD.value: opts.refusal_weights.get(
                            RefusalReason.OVERCROWD.value, 0.5
                        ),
                    }
                total_cause = sum(causes.values())
                if total_cause > 0.0:
                    for key, value in causes.items():
                        refusals[key] = (
                            refusals.get(key, 0.0) + lost * value / total_cause
                        )

    # Заполненность сегмента — отношение пикового потока к часовой
    # провозной способности **в том же окне** (§8.3). Ключ ёмкости включает
    # окно: провозная способность зависит от интервала, а интервал — от окна.
    load_factor: dict[tuple[Any, int, int], float] = {}
    for segment, value in segment_load_window_loads.items():
        route_id, dir_idx, seg_idx, code = segment
        capacity = segment_capacity.get(segment, 0.0)
        if capacity <= 0.0:
            continue
        peak = value * float(opts.peak_hour_share)
        key = (route_id, dir_idx, seg_idx)
        load_factor[key] = max(load_factor.get(key, 0.0), peak / capacity)

    satisfaction = (100.0 * well / offered) if offered > 0.0 else 0.0
    penalty = (100.0 * (well_no_cap - well) / offered) if offered > 0.0 else 0.0

    # Округление до фиксированной точности на границе уровня (§32.5). Канонический
    # порядок обхода убирает зависимость от порядка во входе; округление убирает
    # остаточный шум суммирования, который иначе доезжает до отчёта и сравнений.
    def fixed(value: float) -> float:
        return round(float(value), FIXED_POINT_DECIMALS)

    return PassengerResult(
        total_demand=fixed(total_production),
        transit_trips=fixed(transit),
        walk_trips=fixed(walk_trips),
        car_trips=fixed(car),
        transfer_trips=fixed(transfer_trips),
        coverage=fixed((covered / total_production) if total_production > 0.0 else 0.0),
        satisfaction=fixed(satisfaction),
        overcrowd_penalty_pp=fixed(penalty),
        boardings={key: fixed(value) for key, value in boardings.items()},
        boardings_by_mode={
            key: fixed(value) for key, value in boardings_by_mode.items()
        },
        segment_loads={
            key: fixed(value) for key, value in segment_loads.items()
        },
        segment_load_factor={
            key: fixed(value) for key, value in load_factor.items()
        },
        refusals={key: fixed(value) for key, value in refusals.items()},
        uniform_masses=uniform_masses,
        pop_flows=tuple(pop_flows),
    )
