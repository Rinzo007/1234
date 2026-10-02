"""Скимы T1: зона → зона по окнам маршрутизации (§32.3).

Один прогон маршрутизатора обслуживает всех пассажиров зоны;
результат — таблица, из которой T2 берёт лучший вариант для каждой
пары зон без повторного поиска пути.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Sequence

from .params import (
    DemandPoint,
    PassengerOptions,
    ROUTING_WINDOWS,
    RefusalReason,
    W_WAIT,
    W_WALK,
)

from .geo import (
    _haversine_m,
)

from .network import (
    _Network,
    _StopIndex,
    _access_states,
    _journey_from_parent,
    _run_dijkstra,
)

from .time_model import (
    passenger_transit_share,
)


@dataclass(frozen=True, slots=True)
class ZoneSkims:
    """Ским-матрица «зона отправления → зона назначения» (§32.3, шаг 3).

    Таблица, из которой берётся **точный** лучший вариант для всех пассажиров
    с одинаковой парой зон. Приближён единственный элемент: пассажиры одной
    зоны считаются пришедшими из её центра (§32.3) — размер этого
    приближения измеряется проверкой ≤1 % (§32.4).
    """

    #: Ключ ``(зона_отправления, зона_назначения, окно)`` → обобщённое время.
    time: dict[tuple[int, int, str], float]
    #: Число пересадок для того же ключа.
    transfers: dict[tuple[int, int, str], int]
    #: Разложение времени по видам — для качества поездки (§6.7).
    ride: dict[tuple[int, int, str], float]
    walk: dict[tuple[int, int, str], float]
    wait: dict[tuple[int, int, str], float]
    #: Сегменты пути — для заполненности (§8.3).
    segments: dict[tuple[int, int, str], tuple[tuple[Any, int, int], ...]]
    #: Остановки посадки — для показателя станции (§13.5).
    board_stops: dict[tuple[int, int, str], tuple[str, ...]]
    #: Наибольший интервал на пути — для причины «ожидание» (§6.6).
    worst_headway: dict[tuple[int, int, str], float]
    #: Число выполненных прогонов маршрутизатора (счётчик бюджета T1).
    runs: int


@dataclass(frozen=True, slots=True)
class ZoneCenters:
    """Геометрия зон на входе T1: ключ, центр, широта/долгота.

    T1 **не знает о попах** (§33.1: ``sim-t1`` не знает о попах). На вход идёт
    только геометрия — этого достаточно, чтобы построить ским «зона → зона»:
    число прогонов определяется числом зон, а не числом людей (§30.1).
    Отображение «точка спроса → зона» живёт в T2, потому что это знание о
    спросе.
    """

    keys: tuple[str, ...]
    lats: tuple[float, ...]
    lons: tuple[float, ...]

    @property
    def count(self) -> int:
        return len(self.keys)


def build_zone_access(
    net: _Network,
    centers: ZoneCenters,
    stop_index: _StopIndex,
    options: PassengerOptions,
) -> tuple[tuple[tuple[float, int], ...], ...]:
    """Пеший подход от центра каждой зоны — единственное приближение скима.

    Считается один раз на зону и переиспользуется всеми окнами: вход в зону от
    окна не зависит (§5.6a — различаются интервалы, а не пеший подход).
    """
    access: list[tuple[tuple[float, int], ...]] = []
    for index in range(centers.count):
        centroid = DemandPoint(
            key=centers.keys[index],
            lat=centers.lats[index],
            lon=centers.lons[index],
        )
        access.append(tuple(_access_states(net, stop_index, centroid, options)))
    return tuple(access)


def _zone_egress(
    net: _Network,
    stop_index: _StopIndex,
    lat: float,
    lon: float,
    options: PassengerOptions,
) -> list[tuple[float, int]]:
    """Пеший выход из центра зоны — ``(секунды, остановка)``, по остановке.

    Из нескольких остановок зоны берётся ближайшая к её центру: один и тот же
    выход, посчитанный по нескольким точкам (§32.3 — пассажиры зоны приходят
    из её центра).
    """
    walk_speed = max(0.1, float(options.walk_speed_mps))
    limit = min(
        max((float(r) for r in net.radii if r > 0.0), default=0.0),
        float(options.max_walk_to_stop_s) * walk_speed,
    )
    if limit <= 0.0:
        return []
    found: dict[int, float] = {}
    for index in stop_index.nearby(lat, lon, limit):
        radius = net.radii[index]
        if radius <= 0.0:
            continue
        dist = _haversine_m(lat, lon, net.lats[index], net.lons[index])
        if dist > min(radius, limit):
            continue
        previous = found.get(index)
        if previous is None or dist < previous:
            found[index] = dist
    return sorted(
        ((dist / walk_speed, index) for index, dist in found.items()),
        key=lambda item: item[1],
    )


def _compute_skim_matrix(
    net: _Network,
    centers: ZoneCenters,
    zone_access: tuple[tuple[tuple[float, int], ...], ...] | list[list[tuple[float, int]]],
    stop_index: _StopIndex,
    options: PassengerOptions,
    origin_zones: Sequence[int] | None = None,
) -> ZoneSkims:
    """Считает скимы T1: один прогон на зону и окно (§32.3, шаг 2).

    Каждый прогон обслуживает всех пассажиров зоны сразу, поэтому число
    прогонов равно ``зоны × 4 окна`` и не зависит от числа людей.

    ``origin_zones`` ограничивает пересчёт подмножеством зон — это путь
    инкрементального пересчёта (§32.4). Зоны назначения перебираются всегда:
    скимы, которые не пересчитаны, остаются от прошлого прогона и склеиваются
    вызывающим.
    """
    times: dict[tuple[int, int, str], float] = {}
    transfers: dict[tuple[int, int, str], int] = {}
    rides: dict[tuple[int, int, str], float] = {}
    walks: dict[tuple[int, int, str], float] = {}
    waits: dict[tuple[int, int, str], float] = {}
    segments: dict[tuple[int, int, str], tuple[tuple[Any, int, int], ...]] = {}
    boards: dict[tuple[int, int, str], tuple[str, ...]] = {}
    worst: dict[tuple[int, int, str], float] = {}
    runs = 0

    zone_count = centers.count
    # Выход из зоны назначения ищется по координатам центра, а не по
    # совпадению ключей: точки спроса приходят из растра населения с
    # собственными ключами и с остановками не совпадают (§5.2).
    egress: list[list[tuple[float, int]]] = [
        _zone_egress(
            net, stop_index, centers.lats[index], centers.lons[index], options
        )
        for index in range(zone_count)
    ]

    origins = range(zone_count) if origin_zones is None else origin_zones

    for code, _start, _end in ROUTING_WINDOWS:
        for zone_index in origins:
            initial = zone_access[zone_index]
            if not any(stop >= 0 for _walk, stop in initial):
                continue
            best, parent = _run_dijkstra(
                net, initial, f"zone:{zone_index}", options, code
            )
            runs += 1
            if not best:
                continue
            for target_index in range(zone_count):
                candidates = egress[target_index]
                if not candidates:
                    continue
                # Ским берёт лучший вариант по всем выходам из зоны
                # назначения; выход пешком входит в walk (§6.3).
                best_time = math.inf
                chosen_stop = -1
                chosen_walk = 0.0
                for egress_s, stop in candidates:
                    if stop < 0 or stop not in best:
                        continue
                    total = best[stop] + egress_s * W_WALK
                    if total < best_time:
                        best_time = total
                        chosen_stop = stop
                        chosen_walk = egress_s
                if chosen_stop < 0 or not math.isfinite(best_time):
                    continue
                key = (zone_index, target_index, code)
                times[key] = best_time
                journey = _journey_from_parent(
                    net, parent, chosen_stop, chosen_walk
                )
                transfers[key] = journey.transfers if journey else 0
                rides[key] = journey.ride_s if journey else 0.0
                walks[key] = journey.walk_s if journey else 0.0
                waits[key] = journey.wait_s if journey else 0.0
                segments[key] = journey.segments if journey else ()
                boards[key] = tuple(
                    leg.from_key
                    for leg in (journey.legs if journey else ())
                    if leg.kind == "board"
                )
                worst[key] = max(
                    (
                        net.route_schedule[route_id].minutes_for(code) or 0.0
                        for route_id in (journey.boardings if journey else ())
                        if route_id in net.route_schedule
                    ),
                    default=0.0,
                )

    return ZoneSkims(
        time=times,
        transfers=transfers,
        ride=rides,
        walk=walks,
        wait=waits,
        segments=segments,
        board_stops=boards,
        worst_headway=worst,
        runs=runs,
    )


@dataclass(frozen=True, slots=True)
class T1Result:
    """Результат одного прогона T1."""

    skims: ZoneSkims
    #: Отпечаток сети, для которой посчитано (§32.4).
    network_hash: str
    #: Число линий в сети — им определяется выбор стратегии пересчёта (§32.4).
    line_count: int
    #: Зоны, которые пересчитаны в этом прогоне. Пусто означает «взято из
    #: снимка без пересчёта».
    recomputed_zones: tuple[int, ...]
    #: Почему выбрана именно такая стратегия — для отчёта, а не для логики.
    strategy: str

    @property
    def runs(self) -> int:
        return self.skims.runs


@dataclass(frozen=True, slots=True)
class SkimSnapshot:
    """Снимок T1 для инкрементального пересчёта (§32.4).

    Хранит не только скимы, но и то, по чему определяется «зона затронута»:
    множество остановок в пешем охвате зоны и подпись обслуживающих их линий.
    Без этого сравнения нечем было бы делать, и пересчёт ушёл бы в «пересчитать
    всё», то есть в исходный прямой расчёт.
    """

    network_hash: str
    line_count: int
    skims: ZoneSkims
    #: Охват каждой зоны: индексы остановок в пределах её пешего радиуса.
    zone_catchments: tuple[frozenset[int], ...]
    #: Подпись обслуживания: для каждой зоны — (линии охвата, интервалы).
    zone_service: tuple[str, ...]

    @property
    def zone_count(self) -> int:
        return len(self.zone_catchments)


@dataclass(frozen=True, slots=True)
class IncrementalPlan:
    """Решение о стратегии пересчёта T1 (§32.4).

    | Размер сети | Стратегия |
    |---|---|
    | < 40 линий | полный пересчёт |
    | ≥ 40 линий | только зоны, чей охват затронут |
    """

    #: Пересчитать целиком.
    full: bool
    #: Подмножество зон к пересчёту; имеет смысл только при ``full is False``.
    zones: tuple[int, ...]
    #: Человекочитаемое объяснение решения — попадает в отчёт.
    reason: str

    @property
    def zone_budget(self) -> int:
        """Сколько зон предписано пересчитать."""
        return -1 if self.full else len(self.zones)


#: Порог размера сети для инкрементального пересчёта (§32.4). Правило из
#: источника [T]: «большие сети пересчитываются намного быстрее после
#: изменения линии или станции; меньшие сети сохраняют прямой расчёт, который
#: для их размера быстрее».
INCREMENTAL_LINE_THRESHOLD: int = 40


def _zone_catchments(
    net: _Network,
    centers: ZoneCenters,
    stop_index: _StopIndex,
    options: PassengerOptions,
) -> tuple[frozenset[int], ...]:
    """Остановки в пешем охвате каждой зоны.

    Зона затронута изменением, если изменился её охват. Радиус берётся тот же,
    что и в маршрутизации, — иначе «затронута» окажется зона, до которой
    пассажир всё равно не дойдёт пешком.
    """
    walk_speed = max(0.1, float(options.walk_speed_mps))
    limit = min(
        max((float(r) for r in net.radii if r > 0.0), default=0.0),
        float(options.max_walk_to_stop_s) * walk_speed,
    )
    if limit <= 0.0:
        return tuple(frozenset() for _ in range(centers.count))
    result: list[frozenset[int]] = []
    for index in range(centers.count):
        result.append(
            frozenset(
                stop_index.nearby(centers.lats[index], centers.lons[index], limit)
            )
        )
    return tuple(result)


def _zone_service(
    net: _Network,
    catchments: tuple[frozenset[int], ...],
) -> tuple[str, ...]:
    """Подпись обслуживания для каждой зоны: линии охвата и их интервалы.

    В подпись входит интервал **по каждому окну**, потому что снимок T1
    различается, если изменилось окно, а не сама линия.
    """
    result: list[str] = []
    for stops in catchments:
        routes: set[Any] = set()
        for stop in stops:
            routes |= net.stop_routes[stop]
        parts: list[str] = []
        for route_id in sorted(routes, key=lambda item: f"{type(item).__name__}:{item}"):
            schedule = net.route_schedule.get(route_id)
            headways = "-".join(
                "" if schedule is None or schedule.minutes_for(code) is None
                else repr(schedule.minutes_for(code))
                for code, _start, _end in ROUTING_WINDOWS
            )
            parts.append(f"{type(route_id).__name__}:{route_id}@{headways}")
        result.append("|".join(parts))
    return tuple(result)


def plan_incremental(
    net: _Network,
    centers: ZoneCenters,
    stop_index: _StopIndex,
    options: PassengerOptions,
    snapshot: SkimSnapshot | None,
) -> IncrementalPlan:
    """Решает, что пересчитывать: всё или только затронутые зоны (§32.4).

    Порядок решения — по документу, а не по удобству:

    1. Меньше порога линий — полный пересчёт: для малой сети он быстрее.
    2. Се��ь не изменилась — нечего пересчитывать.
    3. Изменилась — сравниваем охват и обслуживание по зонам и пересчитываем
       только те, что затронуты.
    4. **Ни одна зона не затронута, а сеть изменилась** — пересчитываем всё.

    Пункт 4 — страховка, а не формальность. Проверка затронутости опирается на
    охват зоны и линии, обслуживающие её остановки. Изменение, которое
    расширяет достижимость, не касаясь этих остановок, такой проверке не
    видно; вместо тихо устаревших скимов лучше честный полный пересчёт. Цена
    ошибки в эту сторону — лишнее время, в обратную — неверные числа в отчёте.
    """
    line_count = len(net.route_schedule)
    if snapshot is None:
        return IncrementalPlan(True, (), "первый расчёт на этой сети")
    if line_count < INCREMENTAL_LINE_THRESHOLD:
        return IncrementalPlan(
            True,
            (),
            f"в сети {line_count} линий, порог инкрементального пересчёта — "
            f"{INCREMENTAL_LINE_THRESHOLD}: прямой расчёт быстрее",
        )
    if snapshot.network_hash == _fingerprint(net):
        return IncrementalPlan(False, (), "сеть не изменилась")

    catchments = _zone_catchments(net, centers, stop_index, options)
    service = _zone_service(net, catchments)
    zones: list[int] = []
    for index in range(centers.count):
        if index >= snapshot.zone_count:
            zones.append(index)
            continue
        if catchments[index] != snapshot.zone_catchments[index]:
            zones.append(index)
        elif service[index] != snapshot.zone_service[index]:
            zones.append(index)
    if not zones:
        return IncrementalPlan(
            True,
            (),
            "сеть изменилась, но ни одна зона не признана затронутой: "
            "пересчитываем всё, чтобы не отдать устаревшие скимы",
        )
    return IncrementalPlan(
        False,
        tuple(sorted(zones)),
        f"затронуто {len(zones)} зон из {centers.count} при {line_count} линиях",
    )


def _fingerprint(net: _Network) -> str:
    from .cache import network_fingerprint

    return network_fingerprint(net)


def _merge_skims(previous: ZoneSkims, fresh: ZoneSkims) -> ZoneSkims:
    """Склеивает пересчитанные зоны с оставшимися от прошлого прогона."""
    return ZoneSkims(
        time={**previous.time, **fresh.time},
        transfers={**previous.transfers, **fresh.transfers},
        ride={**previous.ride, **fresh.ride},
        walk={**previous.walk, **fresh.walk},
        wait={**previous.wait, **fresh.wait},
        segments={**previous.segments, **fresh.segments},
        board_stops={**previous.board_stops, **fresh.board_stops},
        worst_headway={**previous.worst_headway, **fresh.worst_headway},
        runs=fresh.runs,
    )


def compute_zone_skims(
    net: _Network,
    centers: ZoneCenters,
    stop_index: _StopIndex,
    options: PassengerOptions,
    *,
    zone_access: tuple[tuple[tuple[float, int], ...], ...] | None = None,
    snapshot: SkimSnapshot | None = None,
) -> T1Result:
    """Скимы T1 по геометрии зон — чистая функция (§33.1).

    T1 не знает о попах: на входе только центры зон, на выходе — таблица
    «зона → зона». При ``snapshot`` выполняется инкрементальный пересчёт по
    правилу §32.4 (порог :data:`INCREMENTAL_LINE_THRESHOLD` линий).
    """
    if zone_access is None:
        zone_access = build_zone_access(net, centers, stop_index, options)

    plan = plan_incremental(net, centers, stop_index, options, snapshot)
    if snapshot is not None and not plan.full:
        fresh = _compute_skim_matrix(
            net,
            centers,
            zone_access,
            stop_index,
            options,
            origin_zones=plan.zones,
        )
        skims = _merge_skims(snapshot.skims, fresh)
        recomputed = plan.zones
    elif snapshot is not None and plan.full and len(plan.zones) == 0:
        # Изменение не распознано — честный полный пересчёт (см. plan_incremental).
        recomputed = tuple(range(centers.count))
        fresh = _compute_skim_matrix(
            net, centers, zone_access, stop_index, options
        )
        skims = fresh
    else:
        recomputed = tuple(range(centers.count))
        fresh = _compute_skim_matrix(
            net, centers, zone_access, stop_index, options
        )
        skims = fresh

    return T1Result(
        skims=skims,
        network_hash=_fingerprint(net),
        line_count=len(net.route_schedule),
        recomputed_zones=recomputed,
        strategy=plan.reason,
    )


def capture_snapshot(
    net: _Network,
    centers: ZoneCenters,
    stop_index: _StopIndex,
    options: PassengerOptions,
    skims: ZoneSkims,
) -> SkimSnapshot:
    """Снимок T1 для последующего инкрементального пересчёта."""
    catchments = _zone_catchments(net, centers, stop_index, options)
    return SkimSnapshot(
        network_hash=_fingerprint(net),
        line_count=len(net.route_schedule),
        skims=skims,
        zone_catchments=catchments,
        zone_service=_zone_service(net, catchments),
    )


@dataclass(frozen=True, slots=True)
class _ZoneOutcome:
    """Результат T2 для одной пары зон в одном окне.

    Одинаков для всех пассажиров этой пары — это и есть смысл скима (§32.3):
    пассажир получает «точный лучший вариант» без повторного поиска пути.
    """

    gen_time: float
    transfers: int
    quality: float
    #: Минимальная часовая ёмкость на пути пары зон. Сравнивается с пиковым
    #: потоком в цикле T2, потому что поток у разных пар разный, а ёмкость
    #: у пары одна (§6.7, §8.3).
    min_capacity: float
    causes: dict[str, float]
    segments: tuple[tuple[Any, int, int], ...]
    board_stops: tuple[str, ...]


def _zone_outcome(
    skim: ZoneSkims,
    net: _Network,
    segment_capacity: dict[tuple[Any, int, int, str], float],
    options: PassengerOptions,
    origin_zone: int,
    dest_zone: int,
    window: str,
) -> _ZoneOutcome | None:
    """Считает исход пары зон: время, качество, потерю и её причины.

    Возвращает ``None``, если пути нет. Причины «далеко до остановки» и
    «нет пути» обрабатываются на уровне пары OD, потому что зависят от
    покрытия конкретных точек, а не от пары зон.
    """
    key = (origin_zone, dest_zone, window)
    gen_time = skim.time.get(key)
    if gen_time is None or not math.isfinite(gen_time):
        return None

    transfers = skim.transfers.get(key, 0)
    ride = skim.ride.get(key, 0.0)
    walk = skim.walk.get(key, 0.0)
    wait = skim.wait.get(key, 0.0)

    # Качество поездки [Н]: доля езды в общем времени со штрафом за пересадки.
    # §6.7 требует взвешивать «обслужено хорошо», но форму веса не публикует.
    total_s = ride + walk + wait
    ride_share = (ride / total_s) if total_s > 0.0 else 0.0
    quality = min(
        1.0, ride_share / max(1e-9, float(options.target_ride_share))
    )
    quality /= 1.0 + float(options.transfer_penalty) * max(0, transfers - 1)

    segments = skim.segments.get(key, ())
    peak_min_cap = math.inf
    for segment in segments:
        peak_min_cap = min(
            peak_min_cap, segment_capacity.get((*segment, window), math.inf)
        )
    # Переполнение проверяется на паре зон по **минимальной** ёмкости на пути:
    # переполнен один участок — переполнена поездка (§6.7, §8.3).
    has_capacity = math.isfinite(peak_min_cap) and peak_min_cap > 0.0

    worst_headway = skim.worst_headway.get(key, 0.0)
    weights = options.refusal_weights
    causes: dict[str, float] = {}
    # «Переполнение» сюда **не** попадает, и это не упущение.
    #
    # Переполнение — единственная причина, которая не свойственна паре зон, а
    # возникает при сравнении потока с ёмкостью, то есть в T2, где поток уже
    # известен. Здесь (T1) известно только, что ёмкость посчитана, а это не
    # то же самое, что «вагон полон».
    #
    # Раньше причина начислялась по признаку has_capacity, то есть всегда, когда
    # ёмкость известна. На реальном городе это давало «Переполнение» как 45 %
    # потерь при нулевом штрафе за переполнение: показатель знал, что вагоны
    # пусты, а список причин уводил игрока добавлять вместимость туда, где её
    # не хватало. T2 добавляет причину сам, когда поток действительно превысил
    # ёмкость.
    if transfers >= 2:
        causes[RefusalReason.TRANSFER.value] = weights.get(
            RefusalReason.TRANSFER.value, 0.3
        )
    if (
        wait > 0.0
        and total_s > 0.0
        and (wait * W_WAIT / total_s) > 0.5
        and worst_headway >= float(options.slow_headway_min)
    ):
        causes[RefusalReason.WAITING.value] = weights.get(
            RefusalReason.WAITING.value, 0.3
        )
    causes[RefusalReason.CAR_BETTER.value] = weights.get(
        RefusalReason.CAR_BETTER.value, 0.2
    )

    return _ZoneOutcome(
        gen_time=gen_time,
        transfers=transfers,
        quality=quality,
        min_capacity=peak_min_cap if has_capacity else 0.0,
        causes=causes,
        segments=segments,
        board_stops=skim.board_stops.get(key, ()),
    )


def _mode_shares(
    gen_transit_s: float,
    gen_car_s: float,
    gen_walk_s: float | None,
    beta_per_s: float,
) -> tuple[float, float, float]:
    """Доли режимов логит-моделью по обобщённому времени (§6.5 без денег).

    Раскрытая форма ``passenger_transit_share``: вызывается на каждую пару
    OD, поэтому здесь без проверок на конечность и без построения словаря.
    Тест сверяет равенство с публичной функцией.
    """
    if gen_transit_s < math.inf and gen_car_s < math.inf:
        best = gen_transit_s if gen_transit_s < gen_car_s else gen_car_s
        if gen_walk_s is not None and gen_walk_s < best:
            best = gen_walk_s
        w_transit = math.exp(-beta_per_s * (gen_transit_s - best))
        w_car = math.exp(-beta_per_s * (gen_car_s - best))
        w_walk = (
            math.exp(-beta_per_s * (gen_walk_s - best))
            if gen_walk_s is not None
            else 0.0
        )
        total = w_transit + w_car + w_walk
        return (w_transit / total, w_walk / total, w_car / total)
    if gen_transit_s < math.inf:
        return (1.0, 0.0, 0.0)
    if gen_walk_s is not None:
        return (0.0, 1.0, 0.0)
    return (0.0, 0.0, 1.0)
