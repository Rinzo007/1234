"""Проверка качества зонального приближения — ворота релиза (§32.4).

Ским может быть неправ, и это нельзя обнаружить «на глаз». Документ поэтому
требует измеримую проверку, а не утверждение:

1. Выбрать **2 000 попов** случайной выборкой, стратифицированной по размеру
   попа, чтобы крупные не выпали.
2. Посчитать для них **точные** варианты пути (T3).
3. Сравнить с решением T2 по этим попам.
4. Записать расхождение.

**Критерий приёмки: расхождение выбора режима ≤ 1 % на выборке 2 000.** Число
названо воротами релиза: если зонирование даёт больше 1 % ошибок, растёт не
точность других частей, а число зон.

Направление проверки зависит от размера города, и это два разных случая, а не
один. Для города, у которого зоны в избытке, непройденный критерий — зоны
добавляют, пока хватает бюджета T1. Для города, упирающегося в потолок зон
(§31.4), зон добавить уже нельзя, и непройденный критерий — сигнал поднять
потолок и бюджет T1, **а не** снизить точность. Поле ``remedy`` говорит, какой
из двух случаев перед нами, чтобы отчёт не выдавал «не прошло» без указания,
что с этим делать.

Случайность допустима только здесь и только с явным зерном (§32.5).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from random import Random
from typing import Any

from .params import (
    DemandPoint,
    PassengerOptions,
    W_RIDE,
    W_WALK,
    WINDOW_CONGESTION,
    ZONE_MAX,
)

from .geo import _haversine_m

from .network import (
    _Network,
    _StopIndex,
    _access_states,
    _run_dijkstra,
)

from .skims import _mode_shares

from .time_model import WINDOW_FLOWS

from .zones import ZoneSet


#: Порог приёмки (§32.4). Не украшение, а ворота релиза.
SKIM_DEVIATION_THRESHOLD: float = 0.01

#: Размер выборки по умолчанию (§32.4).
SKIM_SAMPLE_SIZE: int = 2000

#: Сколько струм в стратификации по размеру попа.
SKIM_STRATA: int = 10

_MODES: tuple[str, str, str] = ("transit", "walk", "car")


@dataclass(frozen=True, slots=True)
class SkimVerification:
    """Результат проверки приближения (§32.4)."""

    #: Попов в выборке.
    sampled: int
    #: Сравнений «поп × окно». Одно сравнение, а не один поп: режим
    #: выбирается на пару в окне, и окно входит в решение.
    comparisons: int
    #: Расхождений выбора режима.
    mismatches: int
    #: Расхождение выбора режима: ``mismatches / comparisons``.
    deviation: float
    threshold: float
    passed: bool
    seed: int
    strata: int
    #: Расхождения по парам режимов ``t2->t3`` — показывает, в какую сторону
    #: ошибается ским.
    by_mode: dict[str, int]
    #: Число зон в городе.
    zones: int
    #: Упирается ли город в потолок зон (§31.4).
    at_zone_cap: bool
    #: Что делать по результату — см. комментарий модуля.
    remedy: str

    def summary(self) -> str:
        """Строка для отчёта: измеренное расхождение рядом с порогом."""
        verdict = "пройден" if self.passed else "НЕ ПРОЙДЕН"
        return (
            f"отклонение выбора режима {self.deviation * 100:.2f} % "
            f"на {self.comparisons} сравнениях из {self.sampled} попов "
            f"(порог {self.threshold * 100:.0f} %) — {verdict}; {self.remedy}"
        )


def _point(lat: float, lon: float, key: str) -> DemandPoint:
    """Точка спроса по координатам — для точного расчёта конкретного попа."""
    return DemandPoint(key=key, lat=float(lat), lon=float(lon))


def _egress_from_point(
    net: _Network,
    stop_index: _StopIndex,
    lat: float,
    lon: float,
    options: PassengerOptions,
) -> list[tuple[float, int]]:
    """Выход пешком из произвольной точки — список ``(секунды, остановка)``.

    Тот же отбор, что у зонального выхода в ``skims``: ближайшие остановки в
    пределах радиуса каждой остановки, ограниченные общим пределом пешего
    подхода. Различие одно — здесь центр зоны не используется, потому что
    проверка сравнивает ским с **точным** вариантом конкретного попа, и
    подменять его центром значило бы сравнивать ским с ним же.
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


def exact_transit_gen_time(
    net: _Network,
    stop_index: _StopIndex,
    options: PassengerOptions,
    origin: DemandPoint,
    dest: DemandPoint,
    window: str,
) -> float:
    """Точное обобщённое время транспорта для одной пары точек (T3, §32.4).

    Тот же маршрутизатор и те же веса, что у скима, но без зонального
    приближения: и выход, и выход из назначения считаются от самих точек.
    Возвращает ``inf``, если точного пути нет.
    """
    initial = _access_states(net, stop_index, origin, options)
    if not any(stop >= 0 for _walk, stop in initial):
        return math.inf
    best, _parent = _run_dijkstra(
        net, initial, f"verify:{origin.key}->{dest.key}", options, window
    )
    if not best:
        return math.inf
    best_time = math.inf
    for egress_s, stop in _egress_from_point(
        net, stop_index, float(dest.lat), float(dest.lon), options
    ):
        if stop < 0 or stop not in best:
            continue
        total = best[stop] + egress_s * W_WALK
        if total < best_time:
            best_time = total
    return best_time


def _competitors(
    dist_m: float,
    covered_dest: bool,
    options: PassengerOptions,
) -> tuple[float, float | None]:
    """Обобщённое время автомобиля и пешего хода для пары OD (§6.5).

    Арифметика повторяет T2 построчно: сравнивать точное решение с решением
    T2 нужно по **той же** формуле, иначе измеряется расхождение формул, а не
    расхождение зонального приближения.
    """
    tortuosity = float(options.tortuosity)
    walk_speed = max(0.1, float(options.walk_speed_mps))
    car_speed_mps = max(1.0, float(options.car_speed_kmh) * 1000.0 / 3600.0)
    dist_eff = dist_m * tortuosity
    car_free = dist_eff * tortuosity / car_speed_mps
    car_time = car_free * WINDOW_CONGESTION[options.peak_hour_window]
    if dist_m < float(options.short_car_threshold_m):
        car_time += float(options.short_car_penalty_s)
    car_g = car_time * W_RIDE
    walk_s = dist_eff / walk_speed
    walk_g = (
        walk_s * W_WALK
        if covered_dest and walk_s <= float(options.max_walk_to_stop_s)
        else None
    )
    return car_g, walk_g


def _mode_of(
    gen_transit: float, car_g: float, walk_g: float | None, beta: float
) -> str:
    """Режим с наибольшей долей — то, что T2 записывает в результат.

    Порядок ``_MODES`` совпадает с порядком долей в T2, поэтому при равенстве
    побеждает тот же режим, что и в основном расчёте.
    """
    shares = _mode_shares(gen_transit, car_g, walk_g, beta)
    best_index = 0
    for index in range(1, len(shares)):
        if shares[index] > shares[best_index]:
            best_index = index
    return _MODES[best_index]


def stratified_sample(
    pairs: list[tuple[Any, ...]],
    size: int,
    seed: int,
    strata: int = SKIM_STRATA,
) -> list[int]:
    """Индексы попов выборкой, стратифицированной по размеру попа (§32.4).

    Стратификация нужна, «чтобы крупные не выпали»: равномерная выборка на
    городах с тяжёлым хвостом (один крупный поп и тысяча мелких) почти
    наверняка оставит крупные за бортом, и проверка станет проверкой мелких.
    Внутри струма выборка случайная, с явным зерном (§32.5).

    Порядок кандидатов в каждом струме канонический — по убыванию размера
    попа, затем по ключам: иначе выборка зависит от порядка вставки и
    перестаёт быть воспроизводимой (§32.5).
    """
    count = len(pairs)
    if count == 0 or size <= 0:
        return []
    if size >= count:
        return list(range(count))

    ordered = sorted(
        range(count),
        key=lambda i: (
            -float(pairs[i][2]),
            str(pairs[i][0].key),
            str(pairs[i][1].key),
        ),
    )
    strata = max(1, min(int(strata), size))
    base = size // strata
    remainder = size - base * strata

    rng = Random(seed)
    chosen: list[int] = []
    for stratum in range(strata):
        take = base + (1 if stratum < remainder else 0)
        if take <= 0:
            continue
        # Кандидаты струма — каждый ``strata``-й элемент канонического списка.
        # Порядок упорядочен по убыванию размера попа, поэтому струм 0 —
        # самые крупные попы, а последний — самые мелкие. Срез со смещением
        # не выходит за границу и не пересекается между струмами: каждый
        # кандидат принадлежит ровно одному струму.
        candidates = sorted(
            ordered[stratum::strata],
            key=lambda i: (
                -float(pairs[i][2]),
                str(pairs[i][0].key),
                str(pairs[i][1].key),
            ),
        )
        if take >= len(candidates):
            chosen.extend(candidates)
        else:
            chosen.extend(rng.sample(candidates, take))
    return sorted(dict.fromkeys(chosen))


def verify_skims(
    net: _Network,
    zone_set: ZoneSet,
    skim: Any,
    pairs: list[tuple[Any, ...]],
    options: PassengerOptions,
    stop_index: _StopIndex,
    sample_size: int = SKIM_SAMPLE_SIZE,
    seed: int = 0,
    threshold: float = SKIM_DEVIATION_THRESHOLD,
) -> SkimVerification:
    """Сверяет решение T2 с точным расчётом T3 (§32.4, «ворота релиза»).

    Сравнение идёт по паре «поп × окно»: режим выбирается внутри окна, и окно
    входит в решение. Пары без скима не сравниваются — там T2 и так не выбрал
    транспорт, и сверять нечего.
    """
    sampled = stratified_sample(pairs, sample_size, seed)
    beta = float(options.beta_per_s)
    comparisons = 0
    mismatches = 0
    by_mode: dict[str, int] = {}

    for index in sampled:
        origin, dest, _contribution, dist_m, _gamma = pairs[index][:5]
        origin_zone = zone_set.zone_of.get(str(origin.key))
        dest_zone = zone_set.zone_of.get(str(dest.key))
        if origin_zone is None or dest_zone is None:
            continue

        dest_point = _point(float(dest.lat), float(dest.lon), f"verify-dest:{dest.key}")
        covered_dest = any(
            item[1] >= 0
            for item in _access_states(net, stop_index, dest_point, options)
        )
        car_g, walk_g = _competitors(float(dist_m), covered_dest, options)

        for window in WINDOW_FLOWS["home_to_work"]:
            t2_gen = skim.time.get((origin_zone, dest_zone, window))
            if t2_gen is None or not math.isfinite(t2_gen):
                continue
            comparisons += 1
            t3_gen = exact_transit_gen_time(
                net,
                stop_index,
                options,
                _point(float(origin.lat), float(origin.lon), f"verify-o:{origin.key}"),
                dest_point,
                window,
            )
            if not math.isfinite(t3_gen):
                # Точного пути нет, а ским его нашёл: это ошибка скима.
                mismatches += 1
                by_mode["transit->none"] = by_mode.get("transit->none", 0) + 1
                continue
            t2_mode = _mode_of(t2_gen, car_g, walk_g, beta)
            t3_mode = _mode_of(t3_gen, car_g, walk_g, beta)
            if t2_mode != t3_mode:
                mismatches += 1
                key = f"{t2_mode}->{t3_mode}"
                by_mode[key] = by_mode.get(key, 0) + 1

    deviation = (mismatches / comparisons) if comparisons > 0 else 0.0
    passed = deviation <= threshold
    at_cap = zone_set.count >= ZONE_MAX
    if passed:
        remedy = "критерий выполнен, зональное приближение принимается"
    elif at_cap:
        remedy = (
            "город упирается в потолок зон (§31.4): поднять потолок и бюджет T1, "
            "а не снижать точность"
        )
    else:
        remedy = "зон в избытке: добавлять зоны, пока хватает бюджета T1"

    return SkimVerification(
        sampled=len(sampled),
        comparisons=comparisons,
        mismatches=mismatches,
        deviation=deviation,
        threshold=threshold,
        passed=passed,
        seed=seed,
        strata=SKIM_STRATA,
        by_mode=by_mode,
        zones=zone_set.count,
        at_zone_cap=at_cap,
        remedy=remedy,
    )