"""Константы и структуры данных пассажирского расчёта.

Слой без зависимостей: только стандартная библиотека. Все числа взяты
из DESIGN.md, каждое — с указанием раздела.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from enum import StrEnum
from functools import cached_property
import math
from typing import Any


# ── Спрос: гравитация (§5.5) ──────────────────────────────────────────

#: Единичная длина: на ней decay = 1 при любом γ (§5.5, безразмерность).
D0_M: float = 1.0

#: Наблюдаемая средняя скорость автоезды, §5.3 (15,6 м/с ≈ 56 км/ч).
V_REF_MPS: float = 15.6

#: Запасная скорость автоезды для городов без дорожного графа, §5.3.
CAR_FREE_SPEED_KMH: float = 56.0

#: Запасная извилистость (отношение длины пути к прямой), §5.3.
TORTUOSITY: float = 1.3

#: Потолок размера попа, §5.3 (медиана ≈ 14 — свойство данных, не константа).
MAX_POP_SIZE: int = 200

#: γ по умолчанию, если категория места неизвестна (§5.5).
GAMMA_DEFAULT: float = 1.0

#: Опубликованные показатели затухания по категориям мест [P4], §5.5.
GAMMA_TABLE: dict[str, float] = {
    "AIR": 0.5,
    "EXT": 0.5,
    "HER": 0.5,
    "PORT": 0.7,
    "AMU": 1.0,
    "BTH": 1.0,
    "CUL": 1.0,
    "EVT": 1.0,
    "MUS": 1.0,
    "NAT": 1.0,
    "ZOO": 1.0,
    "NATURE_PARK": 1.0,
    "ARENA": 1.0,
    "RACETRACK": 1.0,
    "AQU": 1.2,
    "GOV": 1.2,
    "MIL": 1.2,
    "HOS": 1.5,
    "PRK": 1.5,
    "SHP": 1.5,
    "SPO": 1.5,
    "LAKE": 1.5,
    "LIB": 2.0,
    "REL": 2.0,
    "RST": 2.0,
    "UNI": 2.0,
    "SCH": 2.5,
    "CNV": 3.0,
}

#: Жёсткий предел гравитации — свойство генератора, §5.5.
HARD_LIMIT_M: float = 200_000.0

#: Мягкий спад за порогом притяжения: вес делится на 10, §5.5.
BEYOND_MAX_PENALTY: float = 10.0

# ── Время выезда (§5.6) и окна маршрутизации (§5.6a) ─────────────────

#: 11 окон спроса: (начало_ч, конец_ч, вес_дом, вес_работа) [S], §5.6.
DEPARTURE_WINDOWS: tuple[tuple[float, float, float, float], ...] = (
    (0.0, 3.0, 0.15, 0.15),
    (3.0, 6.0, 0.30, 0.30),
    (6.0, 7.0, 1.00, 0.30),
    (7.0, 10.0, 2.50, 0.30),
    (10.0, 11.0, 1.00, 0.80),
    (11.0, 15.0, 0.80, 0.80),
    (15.0, 16.0, 0.80, 1.00),
    (16.0, 19.0, 0.30, 2.50),
    (19.0, 20.0, 0.30, 1.00),
    (20.0, 23.0, 0.30, 0.30),
    (23.0, 24.0, 0.15, 0.15),
)

#: 4 окна маршрутизации: (код, начало_ч, конец_ч), §5.6a.
ROUTING_WINDOWS: tuple[tuple[str, float, float], ...] = (
    ("W0", 0.0, 6.0),
    ("W1", 6.0, 11.0),
    ("W2", 11.0, 16.0),
    ("W3", 16.0, 24.0),
)

#: Доли поездок суток по окнам W0–W3 (сумма обеих колонок §5.6), §5.6a.
ROUTING_SHARES: dict[str, float] = {
    "W0": 0.059,
    "W1": 0.388,
    "W2": 0.224,
    "W3": 0.329,
}

#: Ступенчатый множитель заторов по часам [S], §6.5.1 (ключ — начало часа).
HOURLY_CONGESTION: dict[int, float] = {
    0: 0.80,
    1: 0.80,
    2: 0.80,
    3: 0.90,
    4: 0.90,
    5: 0.90,
    6: 1.25,
    7: 1.50,
    8: 1.50,
    9: 1.50,
    10: 1.25,
    11: 1.00,
    12: 1.00,
    13: 1.00,
    14: 1.00,
    15: 1.25,
    16: 1.50,
    17: 1.50,
    18: 1.50,
    19: 1.25,
    20: 0.90,
    21: 0.90,
    22: 0.90,
    23: 0.80,
}

#: Средний множитель заторов по окнам (взвешен по часам таблицы выше).
WINDOW_CONGESTION: dict[str, float] = {
    "W0": 0.85,
    "W1": 1.40,
    "W2": 1.05,
    "W3": 1.16,
}

# ── Воспринимаемое время (§6.4, исправленные RP-оценки) ───────────────

W_RIDE: float = 1.00
W_WALK: float = 1.67
W_WAIT: float = 1.72
W_LATE: float = 0.45

# ── Параметры построения пути (§6.3) ─────────────────────────────────

#: Применим ли к этому пакету бюджет производительности §32.6.
#:
#: **Нет.** §30.3 отводит расчёт T0–T3 TypeScript (80 % проекта) и помечает
#: Python как язык инструментария городов. Бюджет §32.6 (T1 ≈ 1,0 с на трёх
#: воркерах, T2 = 40 мс) посчитан для TypeScript-рантайма; замер на Python
#: даёт величины на два порядка больше. Это не регресс и не отклонение от
#: документа — это другой язык по решению самого документа.
#:
#: Константа существует, чтобы замер на этом пакете нельзя было случайно
#: прочитать как провал бюджета: расхождение заявлено явно, а не осталось
#: неозвученным. Проверка точности скима (§32.4, ≤ 1 %) при этом остаётся
#: обязательной — она про точность, и скоростью не подменяется.
PERF_TARGET_APPLIES: bool = False

#: Знаков после запятой при округлении итогов расчёта (§32.5).
#:
#: §32.5 требует «суммирование в фиксированном порядке; округление до
#: фиксированной точности на границе каждого уровня». Канонический порядок
#: обхода убирает зависимость от порядка во входе, а округление убирает
#: остаточный шум суммирования: два порядка обхода, совпадающие по
#: каноническому, всё равно могут разойтись на единицу в последнем разряде
#: из-за группировки слагаемых.
#:
#: Девять знаков выбраны как «достаточно мало, чтобы убрать шум, и достаточно
#: много, чтобы не выдавать несуществующую точность». При городском масштабе
#: (десятки тысяч поездок в день) это примерно 10⁻¹² от величины — на 4–5
#: порядков ниже любой различимой разницы. Округление не вносит смысла, оно
#: только делает расхождение видимым или отсутствующим.
FIXED_POINT_DECIMALS: int = 9

#: ``MAX_TRANSFERS`` = 8, а не 4 (§6.3). Первая редакция таблицы давала 4
#: (§2.5); §6.2 разрешает конфликт явно: наблюдаемая сходимость RAPTOR — 8,4
#: раунда, поэтому предел 8 не отсекает достижимые пути, а 4 отсекал бы.
MAX_TRANSFERS: int = 8
MAX_WALK_TO_STOP_S: float = 45.0 * 60.0
MAX_TRANSFER_WALK_S: float = 10.0 * 60.0
WALK_SPEED_MPS: float = 1.0
ARRIVAL_GAP_S: float = 50.0
STATION_GROUP_M: float = 150.0

# ── Режимы (§7.1) ─────────────────────────────────────────────────────
#
# Скорости — эксплуатационные для уличного движения (автобус 18,
# трамвай 19); метро 40 — проектное среднее внутри «до 70» [T];
# рельс 58 — движение по земле [T]; вода/фуникулёр — наше [Н].

MODE_PARAMS: dict[str, dict[str, float]] = {
    "bus": {
        "speed_kmh": 18.0,
        "walk_radius_m": 500.0,
        "vehicle_capacity": 90.0,
        "default_headway_min": 10.0,
        "min_headway_min": 3.0,
        "dwell_s": 15.0,
    },
    "tram": {
        "speed_kmh": 19.0,
        "walk_radius_m": 600.0,
        "vehicle_capacity": 260.0,
        "default_headway_min": 10.0,
        "min_headway_min": 3.0,
        "dwell_s": 20.0,
    },
    "metro": {
        "speed_kmh": 40.0,
        "walk_radius_m": 800.0,
        "vehicle_capacity": 800.0,
        "default_headway_min": 5.0,
        "min_headway_min": 2.0,
        "dwell_s": 30.0,
    },
    "rail": {
        "speed_kmh": 58.0,
        "walk_radius_m": 1500.0,
        "vehicle_capacity": 980.0,
        "default_headway_min": 20.0,
        "min_headway_min": 3.0,
        "dwell_s": 40.0,
    },
    "water": {
        "speed_kmh": 20.0,
        "walk_radius_m": 800.0,
        "vehicle_capacity": 200.0,
        "default_headway_min": 30.0,
        "min_headway_min": 5.0,
        "dwell_s": 60.0,
    },
    "cable": {
        "speed_kmh": 12.0,
        "walk_radius_m": 500.0,
        "vehicle_capacity": 60.0,
        "default_headway_min": 10.0,
        "min_headway_min": 3.0,
        "dwell_s": 20.0,
    },
}

_BUS_LIKE = frozenset({"bus", "trolleybus", "electrobus", "minibus", "idea"})
_TRAM_LIKE = frozenset({"tram"})
_METRO_LIKE = frozenset({"metro", "monorail"})
_RAIL_LIKE = frozenset({"train", "rail"})
_WATER_LIKE = frozenset({"water", "ferry"})
_CABLE_LIKE = frozenset({"funicular", "cable"})

#: Дискретные шаги интервалов, §8.2 [T] (0 = движение прекращено).
HEADWAY_STEPS: tuple[float, ...] = (
    2.0,
    3.0,
    4.0,
    5.0,
    6.0,
    8.0,
    10.0,
    12.0,
    15.0,
    20.0,
    30.0,
    40.0,
    60.0,
    90.0,
    120.0,
)

#: Калибровка чувствительности: +10 мин обобщённого времени
#: умножает шансы автомобиля на 1,3 (§6.5.1, проверочная величина).
BETA_PER_S: float = math.log(1.3) / 600.0

#: Поездки короче порога получают штраф «возни» с машиной, §6.8.
SHORT_CAR_THRESHOLD_M: float = 1000.0
SHORT_CAR_PENALTY_S: float = 600.0

#: Доля суточного потока в пиковый час (проектное допущение [Н]).
PEAK_HOUR_SHARE: float = 0.10

#: Доля «полезного» времени в поездке: столько обобщённого времени должно
#: приходиться на езду, чтобы поездка считалась обслуженной хорошо. Своё
#: значение [Н]: §6.7 задаёт три числа и говорит «взвешенные по качеству»,
#: но форму веса не публикует.
TARGET_RIDE_SHARE: float = 0.5

#: Штраф за каждую пересадку сверх первой: поездка с двумя пересадками
#: теряет 15 % качества, с тремя — 26 % (1/1,15²). Своё значение [Н].
TRANSFER_PENALTY: float = 0.15

#: Порог диагностики «время в пути»: транзит хуже автомобиля во столько раз.
TRAVEL_TIME_RATIO: float = 2.0

# ── Зоны (§20.2, §31.4, §32.3) ────────────────────────────────────────

#: `zones = clamp(округл(pop / 5 000), 150, 400)`, округление до кратного 25.
ZONE_POP_PER_ZONE: int = 5_000
ZONE_MIN: int = 150
ZONE_MAX: int = 400
ZONE_ROUND_TO: int = 25

#: Допустимый радиус зоны — меньше пешего подхода в 45 минут (§32.3).
#: При большей зоне ошибка агрегации становится больше ошибки округления
#: интервала, и ским перестаёт быть пригодным (§32.3, приёмка).
ZONE_RADIUS_LIMIT_M: float = 2_700.0


class RefusalReason(StrEnum):
    """Причины отказа сети, §6.6 (без «тарифа» — это §12, экономика)."""

    NO_PATH = "no_path"
    FAR_FROM_STOP = "far_from_stop"
    CAR_BETTER = "car_better"
    TRAVEL_TIME = "travel_time"
    WAITING = "waiting"
    TRANSFER = "transfer"
    OVERCROWD = "overcrowd"


#: Веса причин отказа (§29, вопрос 3 **открыт**).
#: В Takt сказано, что потеря делится между причинами, но не сказано, как;
#: «произведение весов» названо кандидатом, но на реальных данных не
#: подтверждено. Поэтому это параметр, а не константа движка: подбором весов
#: нельзя незаметно изменить отчёт о потерях — только явно объявить новое
#: правило оценки (§27.1, правило о версионировании правил).
REFUSAL_WEIGHTS: dict[str, float] = {
    RefusalReason.OVERCROWD.value: 0.5,
    RefusalReason.TRAVEL_TIME.value: 0.3,
    RefusalReason.WAITING.value: 0.3,
    RefusalReason.TRANSFER.value: 0.3,
    RefusalReason.CAR_BETTER.value: 0.2,
}

#: Причины, выбираемые по одному условию, без смешивания (§6.6).
REFUSAL_EXCLUSIVE: dict[str, float] = {
    RefusalReason.FAR_FROM_STOP.value: 1.0,
    RefusalReason.NO_PATH.value: 1.0,
}


# ── Структуры ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DemandPoint:
    """Точка спроса: жители и рабочие места (§5.2, только отображение масс).

    Числа ``residents``/``jobs`` сами по себе пассажиров не создают —
    поездки рождаются попами через гравитационную модель (§5.5).
    """

    key: str
    lat: float
    lon: float
    residents: float = 0.0
    jobs: float = 0.0


@dataclass(frozen=True, slots=True)
class Zone:
    """Зона — единица агрегации маршрутизации (§20.2, §32.3).

    Зона объединяет точки спроса, живущие рядом. Все пассажиры зоны
    используют **один** прогон маршрутизатора, поэтому число прогонов
    определяется числом зон, а не числом людей или точек (§30.1).
    """

    index: int
    lat: float
    lon: float
    keys: tuple[str, ...]
    residents: float
    jobs: float
    radius_m: float

    @property
    def population(self) -> float:
        """Число людей, ради которых существует зона."""
        return self.residents

@dataclass(frozen=True, slots=True)
class Pop:
    """Группа людей с общим началом и общим концом поездки, §5.3."""

    id: str
    size: float
    origin_key: str
    dest_key: str
    dist_m: float
    gamma: float


@dataclass(frozen=True, slots=True)
class JourneyLeg:
    """Один участок поездки: ``ride`` (в транспорте) или ``walk``."""

    kind: str
    from_key: str
    to_key: str
    time_s: float
    route_id: Any = None
    segment: tuple[Any, int, int] | None = None


@dataclass(frozen=True, slots=True)
class Journey:
    """Поездка на транзите от точки до точки."""

    legs: tuple[JourneyLeg, ...]
    ride_s: float
    walk_s: float
    wait_s: float
    transfers: int
    gen_time_s: float
    boardings: tuple[Any, ...]
    segments: tuple[tuple[Any, int, int], ...]


def passenger_validate_headway(
    minutes: float | None, *, strict: bool = False
) -> float | None:
    """Проверяет интервал по дискретным шагам §8.2.

    ``0``/``None`` → ``None`` (движение прекращено). Нешаговое значение
    в строгом режиме — ``ValueError``, в мягком возвращается как есть.
    """
    if minutes is None:
        return None
    try:
        value = float(minutes)
    except (TypeError, ValueError):
        if strict:
            raise ValueError(f"Некорректный интервал: {minutes!r}")
        return None
    if not math.isfinite(value) or value <= 0.0:
        return None
    if value in HEADWAY_STEPS:
        return value
    if strict:
        raise ValueError(
            f"Интервал {value:g} мин вне шагов {list(HEADWAY_STEPS)} (§8.2)"
        )
    return value

# ── Сеть ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HeadwaySchedule:
    """Расписание линии: интервал в каждом из четырёх окон маршрутизации.

    §5.6a и §32.2 требуют считать T1 **по каждому окну**: у линии интервал
    5 мин утром и 20 мин вечером, и один усреднённый интервал скрыл бы ровно
    то, что пассажир чувствует — что вечером ждать вдвое дольше.

    Хранение — только по окнам маршрутизации, а не по пяти частям суток Takt
    (§8.2): части суток — это интерфейс игрока, окна — единица работы T1.
    """

    by_window: dict[str, float | None]

    @classmethod
    def from_value(cls, value: Any) -> "HeadwaySchedule":
        """Принимает число (интервал на все окна) или словарь по окнам.

        Ключи словаря: ``W0``–``W3``, плюс имена частей суток Takt
        (``04-06``, ``06-09``, ``09-15``, ``15-19``, ``19-24``) — на входе
        их удобнее писать руками. ``0``/``None`` — движение прекращено.
        """
        if value is None or isinstance(value, (int, float)):
            minutes = passenger_validate_headway(value)
            return cls({code: minutes for code, _s, _e in ROUTING_WINDOWS})
        if isinstance(value, HeadwaySchedule):
            return value
        if not isinstance(value, dict):
            raise TypeError(f"Неожиданный тип расписания: {type(value)!r}")
        resolved: dict[str, float | None] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key).strip()
            minutes = passenger_validate_headway(raw_value)
            # Часть суток Takt пересекает окно маршрутизации; берём худший
            # (наибольший) интервал, чтобы окно не обещало частоты, которой
            # нет ни в одной его части.
            for code, start, end in ROUTING_WINDOWS:
                if _takt_period_overlaps(key, start, end):
                    current = resolved.get(code)
                    if current is None:
                        resolved[code] = minutes
                    elif minutes is None or (current is not None and minutes > current):
                        resolved[code] = minutes
            if key in {code for code, _s, _e in ROUTING_WINDOWS}:
                resolved.setdefault(key, minutes)
        for code, _s, _e in ROUTING_WINDOWS:
            resolved.setdefault(code, None)
        return cls(resolved)

    def minutes_for(self, window: str) -> float | None:
        """Интервал в окне; ``None`` — линия в это окно не ходит."""
        return self.by_window.get(window)

    def is_running_in(self, window: str) -> bool:
        """Ходит ли линия в окно (``None`` интервала не считается ходьбой)."""
        return self.by_window.get(window) is not None


#: Границы частей суток Takt (§8.2) для разбора ключей расписания.
_TAKT_PERIODS: tuple[tuple[str, float, float], ...] = (
    ("04-06", 4.0, 6.0),
    ("06-09", 6.0, 9.0),
    ("09-15", 9.0, 15.0),
    ("15-19", 15.0, 19.0),
    ("19-24", 19.0, 24.0),
)


def _takt_period_overlaps(key: str, start: float, end: float) -> bool:
    """Пересекается ли часть суток ``key`` с окном маршрутизации."""
    normalized = key.replace("–", "-").replace(" ", "")
    for period, period_start, period_end in _TAKT_PERIODS:
        if normalized in (period, period.replace("-", "")):
            return period_start < end and start < period_end
    return False


@dataclass(frozen=True)
class PassengerOptions:
    """Настройки расчёта (всё — с дефолтами из DESIGN.md)."""

    tortuosity: float = TORTUOSITY
    car_speed_kmh: float = CAR_FREE_SPEED_KMH
    max_dist_m: float = 50_000.0
    hard_limit_m: float = HARD_LIMIT_M
    walk_speed_mps: float = WALK_SPEED_MPS
    max_walk_to_stop_s: float = MAX_WALK_TO_STOP_S
    max_transfer_walk_s: float = MAX_TRANSFER_WALK_S
    max_transfers: int = MAX_TRANSFERS
    arrival_gap_s: float = ARRIVAL_GAP_S
    beta_per_s: float = BETA_PER_S
    short_car_threshold_m: float = SHORT_CAR_THRESHOLD_M
    short_car_penalty_s: float = SHORT_CAR_PENALTY_S
    peak_hour_share: float = PEAK_HOUR_SHARE
    peak_hour: float = 8.0
    slow_headway_min: float = 20.0
    travel_time_ratio: float = TRAVEL_TIME_RATIO
    target_ride_share: float = TARGET_RIDE_SHARE
    transfer_penalty: float = TRANSFER_PENALTY
    #: Веса причин отказа. §29 вопрос 3 **открыт**: в Takt сказано, что
    #: потеря делится между причинами, но не сказано, как. «Произведение
    #: весов» названо кандидатом, но не подтверждено на реальных данных.
    #: Поэтому это параметр, а не константа движка.
    refusal_weights: dict[str, float] = field(
        default_factory=lambda: dict(REFUSAL_WEIGHTS)
    )
    #: Окно маршрутизации, в котором измеряется автомобиль (§5.6a).
    peak_hour_window: str = "W1"
    trips_per_person: float = 2.0
    default_population: float = 100.0
    #: Переопределение числа зон. ``None`` — формула §20.2. Задаётся только
    #: для проверки (§32.4) и для тестов на малых сетях.
    zone_count: int | None = None
    top_k_destinations: int | None = None
    max_origins: int | None = None
    #: Потолок числа пар OD, которые разрешено построить на месте.
    #:
    #: §18.5 выносит OD-матрицу в инструментарий и хранит её в пакете города
    #: именно потому, что на месте она нечитаема. Построение на месте —
    #: удобство для малых городов и тестов, а не способ для больших: при
    #: 89 138 точках это 7,9 · 10⁹ пар, и такой расчёт не остановить, а
    #: прерывать придётся руками.
    #:
    #: Потолок подобран так, чтобы проходил эталонный город документа
    #: (3 339 точек, ~11 млн пар), и срабатывал раньше, чем расчёт станет
    #: нечитаемым. Превышение — не тихая потеря точности, а ошибка с текстом:
    #: поднимите потолок осознанно или подайте готовую матрицу.
    max_od_pairs: int = 50_000_000
    strict_headway: bool = False


@dataclass(frozen=True)
class PassengerResult:
    """Итог расчёта за средний будний день (без денег, §12 исключён)."""

    total_demand: float
    transit_trips: float
    walk_trips: float
    car_trips: float
    transfer_trips: float
    coverage: float
    satisfaction: float
    overcrowd_penalty_pp: float
    boardings: dict[str, float]
    #: Посадки в день **по режимам** (§28 шаг 7: «сравниваются посадки в день по
    #: режимам и в целом»). Поездка относится к режиму первой ветки её пути:
    #: пересадка с автобуса на метро — это поездка, начавшаяся на автобусе.
    #:
    #: Сумма может быть **меньше** ``transit_trips``, и это верно: поездка,
    #: ским-лучшим которой оказалась чистая пешая ходьба из зоны, входит в долю
    #: транспорта (её обобщённое время оказалось выгодным), но посадок не даёт —
    #: в транспорт никто не садился. Разрыв между поездками и посадками и есть
    #: величина, которую измеряет проверка качества скима (§32.4).
    boardings_by_mode: dict[str, float]
    segment_loads: dict[tuple[Any, int, int], float]
    segment_load_factor: dict[tuple[Any, int, int], float]
    refusals: dict[str, float]
    uniform_masses: bool
    #: Потоки по парам «начало → конец» вместо объектов ``Pop``. Полмиллиона
    #: объектов на город — это больше памяти, чем весь остальной результат,
    #: а §13 нужны только агрегаты. ``pops`` собирает объекты по требованию.
    #: Элемент: ``(начало, конец, дистанция_м, показатель_γ, поток)``.
    pop_flows: tuple[tuple[str, str, float, float, float], ...] = ()

    @cached_property
    def pops(self) -> tuple[Pop, ...]:
        """Попы (группы по 200 человек), собранные из потоков по парам."""
        result: list[Pop] = []
        for origin_key, dest_key, dist_m, gamma, flow in self.pop_flows:
            remaining = flow
            part = 0
            while remaining >= 0.5:
                size = min(float(MAX_POP_SIZE), remaining)
                result.append(
                    Pop(
                        id=f"{origin_key}->{dest_key}#{part}",
                        size=size,
                        origin_key=origin_key,
                        dest_key=dest_key,
                        dist_m=dist_m,
                        gamma=gamma,
                    )
                )
                remaining -= size
                part += 1
        return tuple(result)

    def pop_summary(self) -> dict[str, float]:
        """Сводка по попам без построения объектов."""
        if not self.pop_flows:
            return {"count": 0.0, "total": 0.0, "max_size": 0.0, "pops": 0.0}
        total_people = 0.0
        count = 0.0
        max_size = 0.0
        for _origin, _dest, _dist, _gamma, flow in self.pop_flows:
            total_people += flow
            count += math.ceil(flow / MAX_POP_SIZE) if flow >= 0.5 else 0.0
            if flow > max_size:
                max_size = flow
        return {
            "count": count,
            "total": total_people,
            "max_size": min(max_size, float(MAX_POP_SIZE)),
            "pops": count,
        }
