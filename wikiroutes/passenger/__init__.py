"""Расчёт пассажиропотока городской транспортной сети по DESIGN.md (PULSE).

Модуль реализует пассажирскую часть дизайн-документа: спрос (§5),
воспринимаемое время и выбор режима (§6), зональные скимы (§32.3) и
назначение пассажиров (T2). **Экономика (§12) и строительство (§9) в
модуль не входят.**

Состав пакета:

| Модуль | Слой | Разделы DESIGN.md |
|---|---|---|
| :mod:`.params` | Константы и структуры данных | §5–§8, §20.2 |
| :mod:`.geo` | Расстояния, доступ к полям объектов | §5.3 |
| :mod:`.time_model` | Воспринимаемое время, выбор режима | §6.4, §6.5, §6.8 |
| :mod:`.demand` | Гравитация, OD-матрица, попы | §5.3, §5.5 |
| :mod:`.zones` | Зонирование спроса | §20.2, §31.4, §32.3 |
| :mod:`.network` | Сеть, расписание, поиск пути | §6.2, §6.3, §8.2 |
| :mod:`.skims` | Скимы T1 «зона → зона» | §32.2, §32.3 |
| :mod:`.flow` | Назначение T2, показатели | §6.6, §6.7, §13 |
| :mod:`.cache` | Отпечаток сети, ключ кэша T1 | §32.4 |
| :mod:`.verify` | Проверка приближения ≤ 1 % | §32.4 |

.. warning::

   **Язык. Этот пакет — инструментарий, а не рантайм игры.** §30.3 отводит
   TypeScript 80 % проекта и помечает Python как язык инструментария городов.
   Бюджет §32.6 (T1 ≈ 1,0 с на 3 воркерах) относится к TypeScript-рантайму и
   **к этому пакету неприменим**: замер на Python даёт величины на два порядка
   больше, и это не регресс, а следствие выбора языка. Константа
   :data:`PERF_TARGET_APPLIES` равна ``False`` именно для того, чтобы замер на
   этом пакете нельзя было случайно прочитать как провал бюджета.

   Что из этого следует по существу: числа §32.6 нельзя использовать как
   приёмочные для Python-пакета, а проверка качества скима (:mod:`.verify`)
   остаётся обязательной — она про точность, а не про скорость, и скоростью
   не подменяется.

Архитектурные решения, которые определяют производительность:

- **Зоны, а не точки.** Начала агрегируются в зоны по формуле
  ``clamp(округл(pop / 5000), 150, 400)`` (§20.2). Один прогон
  маршрутизатора обслуживает всех пассажиров зоны, поэтому число прогонов
  равно ``зоны × 4 окна`` и не зависит от числа людей. Расчёт по точкам
  отменён документом как превышающий бюджет.
- **Четыре окна маршрутизации** (§5.6a). Интервал линии хранится по окнам
  W0–W3, поэтому вечерний пик не усредняется с утренним.
- **OD-матрица — шаг инструментария.** Она не зависит от сети игрока и
  хранится в пакете города (``demand_data``, §18.5).
- **Детерминизм** (§32.5). Канонический порядок обхода, никаких
  обращений к времени и случайности.

Отличия от документированной схемы, и почему:

- **Язык пакета.** §30.3: расчёт T0–T3 в TypeScript, Python — инструментарий
  городов. Пакет написан на Python и используется как эталонный расчёт и
  проверка точности; см. предупреждение выше.
- **Частотная модель вместо поминутного расписания.** В проекте 1234 нет
  расписаний рейсов, поэтому ожидание считается как половина интервала плюс
  зазор прибытия (§6.3). Поиск по диапазону отправлений (rRAPTOR) не
  выполняется — документ сам отказывается от него в пользу множителя
  интервала (§6.2, §29 вопрос 7).
- **Денежная составляющая времени равна нулю.** Тариф и стоимость времени
  зависят от дохода и экономики (§6.4, §12), которых здесь нет.
- **Веса причин отказа задаются параметром.** Формула деления потери между
  причинами в документе **открыта** (§29 вопрос 3), поэтому это настройка,
  а не константа.
"""

from __future__ import annotations

from .cache import (
    T1_CACHE_CAPACITY,
    T1CacheKey,
    T1SkimStore,
    WINDOW_INDEX,
    network_fingerprint,
    t1_cache_key,
)
from .demand import ODPair, build_od_pairs, passenger_gravity_weight
from .skims import (
    INCREMENTAL_LINE_THRESHOLD,
    IncrementalPlan,
    SkimSnapshot,
    T1Result,
    ZoneCenters,
    ZoneSkims,
    build_zone_access,
    capture_snapshot,
    compute_zone_skims,
    plan_incremental,
)
from .flow import assign_passengers, calculate_passenger_flow
from .geo import _haversine_m
from .network import passenger_mode_key
from .params import (
    ARRIVAL_GAP_S,
    BETA_PER_S,
    CAR_FREE_SPEED_KMH,
    D0_M,
    DEPARTURE_WINDOWS,
    GAMMA_DEFAULT,
    GAMMA_TABLE,
    HARD_LIMIT_M,
    HEADWAY_STEPS,
    HOURLY_CONGESTION,
    Journey,
    JourneyLeg,
    MAX_POP_SIZE,
    MAX_TRANSFERS,
    MAX_TRANSFER_WALK_S,
    MAX_WALK_TO_STOP_S,
    MODE_PARAMS,
    PassengerOptions,
    PassengerResult,
    PERF_TARGET_APPLIES,
    Pop,
    passenger_validate_headway,
    DemandPoint,
    RefusalReason,
    ROUTING_SHARES,
    ROUTING_WINDOWS,
    STATION_GROUP_M,
    TORTUOSITY,
    V_REF_MPS,
    WALK_SPEED_MPS,
    W_LATE,
    W_RIDE,
    W_WAIT,
    W_WALK,
    WINDOW_CONGESTION,
    ZONE_MAX,
    ZONE_MIN,
    ZONE_POP_PER_ZONE,
    ZONE_RADIUS_LIMIT_M,
    ZONE_ROUND_TO,
    HeadwaySchedule,
    Zone,
)
from .time_model import (
    passenger_car_time_s,
    passenger_congestion_factor,
    passenger_generalized_time,
    passenger_routing_window_of,
    passenger_transit_share,
    passenger_wait_s,
)
from .estimate import (
    BUILT_SURFACE_PRODUCT,
    DEFAULT_M2_PER_JOB,
    SENSITIVITY_STEPS,
    BuiltSurfaceRaster,
    JobsEstimate,
    jobs_from_built_surface,
)
from .population import (
    DEFAULT_MAX_POINTS_PER_KM2,
    DEFAULT_MIN_CELL_POPULATION,
    GHS_POPULATION_PRODUCT,
    PopulationRaster,
    PopulationReading,
    population_points,
    read_population,
)
from .reference import (
    MODEL_BOARDING_DEFINITION,
    BoardingsReference,
    ModeDeviation,
    ReferenceCheck,
    check_against_reference,
    trip_distance_ks,
    trip_distance_wasserstein,
)
from .zones import ZoneSet, build_zones, passenger_zone_count
from .verify import (
    SKIM_DEVIATION_THRESHOLD,
    SKIM_SAMPLE_SIZE,
    SKIM_STRATA,
    SkimVerification,
    exact_transit_gen_time,
    stratified_sample,
    verify_skims,
)

__all__ = [
    # Главный расчёт
    "calculate_passenger_flow",
    # Граница модулей T1/T2 (§33.1)
    "assign_passengers",
    "build_zone_access",
    "capture_snapshot",
    "compute_zone_skims",
    "plan_incremental",
    "IncrementalPlan",
    "SkimSnapshot",
    "T1Result",
    "ZoneCenters",
    "INCREMENTAL_LINE_THRESHOLD",
    # Спрос
    "ODPair",
    "build_od_pairs",
    "passenger_gravity_weight",
    # Зоны
    "Zone",
    "ZoneSet",
    "build_zones",
    "passenger_zone_count",
    # Скимы
    "ZoneSkims",
    # Кэш T1 (§32.4)
    "T1CacheKey",
    "T1SkimStore",
    "T1_CACHE_CAPACITY",
    "WINDOW_INDEX",
    "network_fingerprint",
    "t1_cache_key",
    # Проверка приближения (§32.4)
    "SkimVerification",
    "SKIM_DEVIATION_THRESHOLD",
    "SKIM_SAMPLE_SIZE",
    "SKIM_STRATA",
    "exact_transit_gen_time",
    "stratified_sample",
    "verify_skims",
    # Рабочие места: оценка из застройки, не измерение (§17.3)
    "BuiltSurfaceRaster",
    "JobsEstimate",
    "BUILT_SURFACE_PRODUCT",
    "DEFAULT_M2_PER_JOB",
    "SENSITIVITY_STEPS",
    "jobs_from_built_surface",
    # Население из растра GHSL (§5.2, §18.5)
    "PopulationRaster",
    "PopulationReading",
    "GHS_POPULATION_PRODUCT",
    "DEFAULT_MIN_CELL_POPULATION",
    "DEFAULT_MAX_POINTS_PER_KM2",
    "population_points",
    "read_population",
    # Сверка с эталонной сетью (§28 шаг 7, §29 Q2)
    "BoardingsReference",
    "MODEL_BOARDING_DEFINITION",
    "ModeDeviation",
    "ReferenceCheck",
    "check_against_reference",
    "trip_distance_ks",
    "trip_distance_wasserstein",
    # Допущение о языке (§30.3)
    "PERF_TARGET_APPLIES",
    # Время и режимы
    "passenger_car_time_s",
    "passenger_congestion_factor",
    "passenger_generalized_time",
    "passenger_routing_window_of",
    "passenger_transit_share",
    "passenger_wait_s",
    # Сеть
    "passenger_mode_key",
    "passenger_validate_headway",
    "HeadwaySchedule",
    # Структуры
    "DemandPoint",
    "Journey",
    "JourneyLeg",
    "PassengerOptions",
    "PassengerResult",
    "Pop",
    "RefusalReason",
    # Константы (§-ссылки в params)
    "ARRIVAL_GAP_S",
    "BETA_PER_S",
    "CAR_FREE_SPEED_KMH",
    "D0_M",
    "DEPARTURE_WINDOWS",
    "GAMMA_DEFAULT",
    "GAMMA_TABLE",
    "HARD_LIMIT_M",
    "HEADWAY_STEPS",
    "HOURLY_CONGESTION",
    "MAX_POP_SIZE",
    "MAX_TRANSFERS",
    "MAX_TRANSFER_WALK_S",
    "MAX_WALK_TO_STOP_S",
    "MODE_PARAMS",
    "ROUTING_SHARES",
    "ROUTING_WINDOWS",
    "STATION_GROUP_M",
    "TORTUOSITY",
    "V_REF_MPS",
    "WALK_SPEED_MPS",
    "WINDOW_CONGESTION",
    "W_LATE",
    "W_RIDE",
    "W_WAIT",
    "W_WALK",
    "ZONE_MAX",
    "ZONE_MIN",
    "ZONE_POP_PER_ZONE",
    "ZONE_RADIUS_LIMIT_M",
    "ZONE_ROUND_TO",
]