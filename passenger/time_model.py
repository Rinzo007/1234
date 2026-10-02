"""Воспринимаемое время и выбор режима (§6.4, §6.5, §6.8).

Денежной составляющей нет: тариф и стоимость времени — это §12,
экономика, которая в модуль не входит.
"""

from __future__ import annotations

import math
from typing import Any

from .params import (
    ARRIVAL_GAP_S,
    BETA_PER_S,
    DEPARTURE_WINDOWS,
    HOURLY_CONGESTION,
    PassengerOptions,
    ROUTING_WINDOWS,
    W_LATE,
    W_RIDE,
    W_WAIT,
    W_WALK,
)


def passenger_routing_window_of(hour: float) -> str:
    """Код окна маршрутизации W0–W3 по часу суток, §5.6a.

    Границы групп — 06, 11, 16, 24; часы вне 0–24 приводятся по модулю.
    """
    try:
        hour_value = float(hour) % 24.0
    except (TypeError, ValueError):
        return "W2"
    for code, start, end in ROUTING_WINDOWS:
        if start <= hour_value < end:
            return code
    return "W3" if hour_value >= ROUTING_WINDOWS[-1][2] else "W2"


def passenger_congestion_factor(hour: float) -> float:
    """Ступенчатый множитель заторов по часу, §6.5.1."""
    try:
        key = int(math.floor(float(hour))) % 24
    except (TypeError, ValueError):
        return 1.0
    return HOURLY_CONGESTION.get(key, 1.0)


def passenger_wait_s(headway_min: float, arrival_gap_s: float = ARRIVAL_GAP_S) -> float:
    """Среднее ожидание: половина интервала + зазор прибытия, §6.3."""
    return float(headway_min) * 30.0 + float(arrival_gap_s)


def _window_flows() -> dict[str, dict[str, float]]:
    """Доли поездок по окнам маршрутизации для каждого направления.

    §5.6 даёт 11 окон спроса с разными весами для «дом» и «работа», §5.6a
    группирует их в 4 окна маршрутизации. Асимметрия направлений намеренная
    (§5.6): утром пик из дома, вечером — с работы. Смешивать их нельзя, иначе
    утренняя и вечерняя нагрузка окажутся зеркальными и неверными обе.
    """
    result: dict[str, dict[str, float]] = {
        "home_to_work": {},
        "work_to_home": {},
    }
    totals = {"home_to_work": 0.0, "work_to_home": 0.0}
    for start, end, home, work in DEPARTURE_WINDOWS:
        for code, window_start, window_end in ROUTING_WINDOWS:
            if window_start <= start and end <= window_end:
                result["home_to_work"][code] = (
                    result["home_to_work"].get(code, 0.0) + home
                )
                result["work_to_home"][code] = (
                    result["work_to_home"].get(code, 0.0) + work
                )
                totals["home_to_work"] += home
                totals["work_to_home"] += work
                break
    for direction, values in result.items():
        total = totals[direction]
        if total <= 0.0:
            continue
        for code, value in values.items():
            values[code] = value / total
    return result


#: Доли поездок по окнам W0–W3, отдельно для каждого направления (§5.6a).
WINDOW_FLOWS: dict[str, dict[str, float]] = _window_flows()


def passenger_generalized_time(
    ride_s: float, walk_s: float, wait_s: float, late_s: float = 0.0
) -> float:
    """Воспринимаемое время в секундах, §6.4 (без денег — §12 исключён)."""
    return (
        float(ride_s) * W_RIDE
        + float(walk_s) * W_WALK
        + float(wait_s) * W_WAIT
        + float(late_s) * W_LATE
    )


def passenger_car_time_s(
    dist_m: float,
    hour: float = 8.0,
    options: PassengerOptions | None = None,
) -> float:
    """Время автоезда с заторами и штрафом коротких поездок (§6.8)."""
    opts = options or PassengerOptions()
    try:
        dist = float(dist_m)
    except (TypeError, ValueError):
        return math.inf
    if not math.isfinite(dist) or dist < 0.0:
        return math.inf
    speed_mps = max(1.0, float(opts.car_speed_kmh) * 1000.0 / 3600.0)
    free = dist * float(opts.tortuosity) / speed_mps
    total = free * passenger_congestion_factor(hour)
    if dist < float(opts.short_car_threshold_m):
        total += float(opts.short_car_penalty_s)
    return total


def passenger_transit_share(
    gen_transit_s: float,
    gen_car_s: float,
    gen_walk_s: float | None = None,
    beta_per_s: float = BETA_PER_S,
) -> dict[str, float]:
    """Доли режимов логит-моделью по обобщённому времени (§6.5 без денег).

    Калибровка §6.5.1: при равных временах доли 50/50, разрыв +10 мин
    умножает шансы автомобиля на ≈1,3.
    """
    beta = float(beta_per_s)
    candidates: dict[str, float] = {"transit": float(gen_transit_s), "car": float(gen_car_s)}
    if gen_walk_s is not None:
        try:
            walk_value = float(gen_walk_s)
        except (TypeError, ValueError):
            walk_value = math.inf
        if math.isfinite(walk_value):
            candidates["walk"] = walk_value
    finite = {k: v for k, v in candidates.items() if math.isfinite(v)}
    if not finite:
        return {"transit": 0.0, "car": 1.0, "walk": 0.0}
    if len(finite) == 1:
        only = next(iter(finite))
        return {"transit": 1.0 if only == "transit" else 0.0,
                "car": 1.0 if only == "car" else 0.0,
                "walk": 1.0 if only == "walk" else 0.0}
    best = min(finite.values())
    weights = {k: math.exp(-beta * (v - best)) for k, v in finite.items()}
    total = sum(weights.values())
    result = {"transit": 0.0, "car": 0.0, "walk": 0.0}
    for key, weight in weights.items():
        result[key] = weight / total
    return result
