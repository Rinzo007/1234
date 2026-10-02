"""Спрос: гравитационная модель и OD-матрица (§5.3, §5.5).

OD-матрица не зависит от сети игрока, поэтому строится один раз
в инструментарии и хранится в пакете города.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from .geo import _EARTH_RADIUS_M, _KM_PER_DEG_LAT
from .params import (
    BEYOND_MAX_PENALTY,
    D0_M,
    GAMMA_DEFAULT,
    GAMMA_TABLE,
    HARD_LIMIT_M,
    V_REF_MPS,
    DemandPoint,
    PassengerOptions,
)


def passenger_gravity_weight(
    dist_m: float,
    mass: float,
    gamma: float = GAMMA_DEFAULT,
    *,
    max_dist_m: float = 50_000.0,
    hard_limit_m: float = HARD_LIMIT_M,
) -> float:
    """Вес гравитационной модели ``mass / decay``, §5.5.

    Нулевая дистанция (вырожденный поп) → 0; ``0 < d < d₀`` — как при
    ``d₀`` (вес никогда не превышает массу начала); за ``max_dist_m``
    вес делится на 10; за жёстким пределом — 0; NaN/inf → 0.
    """
    try:
        dist = float(dist_m)
        amount = float(mass)
        exponent = float(gamma)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(dist) or not math.isfinite(amount):
        return 0.0
    if not math.isfinite(exponent):
        exponent = GAMMA_DEFAULT
    if dist <= 0.0 or amount <= 0.0:
        return 0.0
    if dist > hard_limit_m:
        return 0.0
    effective = max(dist, D0_M)
    decay = (effective / D0_M) ** exponent
    if not math.isfinite(decay) or decay <= 0.0:
        return 0.0
    weight = amount / decay
    if dist > max_dist_m:
        weight /= BEYOND_MAX_PENALTY
    return weight if math.isfinite(weight) else 0.0


@dataclass(frozen=True, slots=True)
class ODPair:
    """Пара «начало — конец» с весом гравитационной модели (§5.5).

    Матрица строится **одноразово в инструментарии** и хранится в пакете
    города (`demand_data`, §18.5): она не зависит от сети игрока, поэтому
    пересчитывать её при каждом закрытии года — лишняя работа. §32.3 называет
    кластеризацию зон «одноразовой, в инструментарии», и верно то же про
    матрицу: игра читает готовые пары.
    """

    origin_key: str
    dest_key: str
    weight: float
    dist_m: float
    gamma: float


def build_od_pairs(
    points: list[DemandPoint],
    *,
    categories: dict[str, str] | None = None,
    options: PassengerOptions | None = None,
) -> tuple[tuple[ODPair, ...], float]:
    """Строит OD-матрицу гравитацией (§5.5). Шаг инструментария, не игры.

    :returns: ``(пары, нормировка)``. Доля поездки по паре равна
        ``total_production × weight / нормировка``, поэтому нормировка
        возвращается вместе с парами и должна попасть в пакет города.
    """
    opts = options or PassengerOptions()
    return _od_matrix(points, opts, categories or {})


def _od_matrix(
    demands: list[DemandPoint],
    opts: PassengerOptions,
    categories: dict[str, str],
) -> tuple[tuple[ODPair, ...], float]:
    """Ядро OD-матрицы: пары и нормировка (см. ``build_od_pairs``).

    Каждая пара взвешивается дважды — по массе назначения и по показателю
    γ её категории (§5.5). Симметрия направлений не нужна: обратную пару
    порождает другая точка с рабочими местами.
    """
    origins = [d for d in demands if d.residents > 0.0]
    if opts.max_origins is not None and len(origins) > opts.max_origins:
        origins = sorted(origins, key=lambda d: d.residents, reverse=True)[
            : opts.max_origions
        ]
    destinations = [d for d in demands if d.jobs > 0.0]

    if not destinations:
        if not origins:
            # Данных о населении нет вообще: ни начала, ни конца. Это
            # «город без поездок» — вызывающий сам передал нули, и пустой
            # результат здесь честный ответ, а не ошибка.
            return (), 0.0
        raise ValueError(
            "у точек спроса есть жители, но нет ни одного рабочего места, "
            "поэтому построить OD-матрицу не из чего: масса назначения в "
            "гравитационной модели (§5.5) — это рабочие места, а слой "
            "населения их не содержит. Это не «город без поездок», а "
            "отсутствие данных — и молчать об этом нельзя (§27.1). Либо "
            "подайте рабочие места, либо возьмите оценку и пометьте её как "
            "оценку (§17.3)."
        )
    if not origins:
        raise ValueError(
            "у точек спроса есть рабочие места, но нет ни одного жителя: "
            "поездки без начала не считаются, и построить OD-матрицу не из "
            "чего. Это отсутствие данных о населении, а не нулевой спрос "
            "(§18.5, §27.1)."
        )

    # Потолок числа пар: считать матрицу на месте дольше, чем читаемо, —
    # хуже, чем отказаться с внятным текстом. §18.5 и выносит матрицу в
    # инструментарий именно по этой причине.
    max_pairs = int(getattr(opts, "max_od_pairs", 0) or 0)
    if max_pairs > 0:
        reachable = len(origins) * len(destinations)
        if reachable > max_pairs:
            raise ValueError(
                f"OD-матрица на месте дала бы {reachable:,} пар при потолке "
                f"{max_pairs:,} ({len(origins)} начал × {len(destinations)} "
                "концов). Матрица не зависит от сети игрока и по §18.5 "
                "считается один раз в инструментарии и хранится в пакете "
                "города. Промежуточные пути: задать points_per_km2 при чтении "
                "растра населения (agregирует ячейки в точки и снижает их "
                "число), поднять max_od_pairs осознанно, либо ограничить "
                "max_origins/top_k_destinations."
            )

    speed_mps = max(1.0, float(opts.car_speed_kmh) * 1000.0 / 3600.0)
    tortuosity = float(opts.tortuosity)
    max_dist_m = float(opts.max_dist_m)
    hard_limit_m = float(opts.hard_limit_m)

    # Пространственный индекс: перебор «все начала × все концы» даёт
    # десятки миллионов пар, из которых почти все всё равно за порогом
    # притяжения. Ячейка мельче порога, иначе индекс ничего не отсечёт.
    radius_m = min(hard_limit_m, max_dist_m * float(BEYOND_MAX_PENALTY)) * 3.0
    cell_km = max(0.25, min(radius_m / 1000.0 / 8.0, max_dist_m / 1000.0 / 4.0))
    cell_deg = cell_km / _KM_PER_DEG_LAT
    span = math.ceil(radius_m / 1000.0 / cell_km)
    earth_radius = _EARTH_RADIUS_M

    dest_by_key = {str(point.key): point for point in destinations}
    # Радианы широт считаются один раз: на миллионах пар повторный перевод
    # градусов в радианы съедал заметную долю времени расчёта.
    dest_radians = [
        (
            str(point.key),
            point.jobs,
            math.radians(point.lat),
            math.radians(point.lon),
            math.cos(math.radians(point.lat)),
            categories.get(str(point.key)),
        )
        for point in destinations
    ]
    grid: dict[tuple[int, int], list[tuple[Any, ...]]] = {}
    for record in dest_radians:
        cell = (
            math.floor(record[2] * 180.0 / math.pi / cell_deg),
            math.floor(record[3] * 180.0 / math.pi / cell_deg),
        )
        grid.setdefault(cell, []).append(record)

    pairs: list[ODPair] = []
    norm = 0.0

    for origin in origins:
        scored: list[tuple[float, DemandPoint, float, float]] = []
        ocell = (
            math.floor(origin.lat / cell_deg),
            math.floor(origin.lon / cell_deg),
        )
        near: list[tuple[Any, ...]] = []
        for dx in range(-span, span + 1):
            for dy in range(-span, span + 1):
                bucket = grid.get((ocell[0] + dx, ocell[1] + dy))
                if bucket:
                    near.extend(bucket)
        origin_lat_r = math.radians(origin.lat)
        origin_lon_r = math.radians(origin.lon)
        cos_o = math.cos(origin_lat_r)
        for key, jobs, lat_r, lon_r, cos_d, category in near:
            if key == str(origin.key):
                continue
            half_dlat = math.sin((lat_r - origin_lat_r) / 2.0)
            half_dlon = math.sin((lon_r - origin_lon_r) / 2.0)
            a = half_dlat * half_dlat + cos_o * cos_d * half_dlon * half_dlon
            if a <= 0.0:
                continue  # совпадающие точки: вырожденный поп (§5.5)
            if a > 1.0:
                a = 1.0
            dist_m = 2.0 * earth_radius * math.asin(math.sqrt(a))
            # §5.5: аргумент гравитации — время автоезды, переведённое в
            # метры эталонной скоростью. Это даёт асимметрию «река — мост»,
            # которой нет у метров.
            dist_eff = dist_m * tortuosity / speed_mps * V_REF_MPS
            gamma = GAMMA_TABLE.get(str(category or "").upper(), GAMMA_DEFAULT)
            weight = passenger_gravity_weight(
                dist_eff,
                jobs,
                gamma,
                max_dist_m=max_dist_m,
                hard_limit_m=hard_limit_m,
            )
            if weight > 0.0:
                scored.append((weight, dest_by_key[key], dist_m, gamma))
        if (
            opts.top_k_destinations is not None
            and len(scored) > opts.top_k_destinations
        ):
            scored = sorted(scored, key=lambda item: item[0], reverse=True)[
                : opts.top_k_destinations
            ]
        for weight, dest, dist_m, gamma in scored:
            norm += origin.residents * weight
            pairs.append(
                ODPair(
                    origin_key=str(origin.key),
                    dest_key=str(dest.key),
                    weight=origin.residents * weight,
                    dist_m=dist_m,
                    gamma=gamma,
                )
            )

    # Канонический порядок пар (§32.5): одинаковый город и одинаковые данные
    # обязаны дать одинаковый список независимо от порядка обхода точек.
    pairs.sort(key=lambda pair: (pair.origin_key, pair.dest_key))
    return tuple(pairs), norm
