"""Файл данных спроса города: ``demand_data.json`` (§18.4, §A.5f).

Формат задан документом дословно:

```
{ "points": [ … ], "pops": [ … ] }

точка: { id, location: [долгота, широта], jobs, residents, popIds: [ … ] }
поп:   { id, residenceId, jobId, size, drivingSeconds, drivingDistance }
```

Два свойства этого файла важнее остальных.

**1. Попы генерируются инструментарием, а не игрой.** Движок читает готовые
попы (§A.4: «предел 200 км не переносится в движок, он живёт в шаге, который
создаёт попы»; движок попы не создаёт, а читает). Поэтому файл — артефакт
сборки города, и его содержимое обязано быть воспроизводимым.

**2. Инвариант ``demand_residents_match`` связывает два счёта.** Счётчик
жителей в точке и сумма размеров попов, ссылающихся на эту точку как на место
жительства, обязаны совпадать: это одна и та же величина, посчитанная двумя
путями, и расхождение означало бы, что одна из двух дорог потеряла людей по
дороге. Именно это и проверяет одна из десяти обязательных проверок (§18.5).

**3. Попов больше, чем точек, и это не парадокс.** В поставляемом городе
3 339 точек и 58 551 поп (§A.5f): поп — это группа поездок, а не человек.
Отношение 17,5 попа на точку, медиана размера 14, потолок 200.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from typing import Any, Iterable, Sequence

from passenger.params import CAR_FREE_SPEED_KMH, MAX_POP_SIZE, DemandPoint
from passenger.time_model import passenger_car_time_s


#: Имя файла данных спроса в выпуске.
DEMAND_DATA_FILENAME: str = "demand_data.json"


@dataclass(frozen=True, slots=True)
class Pop:
    """Поп — группа поездок (§A.5f).

    Идентификаторы **неизменяемы**: ``id`` создаётся один раз (§30.3,
    ``ImmutableId``). Пересборка города обязана давать те же идентификаторы, иначе
    результат года перестаёт быть воспроизводимым, а ссылки на попы в отчётах
    перестают значить то же самое.
    """

    id: str
    #: Точка места жительства.
    residence_id: str
    #: Точка работы. ``None`` — назначение не достижимо в пределах порога
    #: притяжения (§5.5). Это состояние города, а не битая ссылка, и оно
    #: хранится как ``null``, а не выдуманным идентификатором.
    job_id: str | None
    #: Сколько человек едет этой поездкой.
    size: float
    #: Время автоезды по прямой, с — посчитано заранее для всех попов (§A.5f).
    driving_seconds: float
    #: Расстояние автоезды по прямой, м.
    driving_distance: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "residenceId": self.residence_id,
            "jobId": self.job_id,
            "size": self.size,
            "drivingSeconds": round(float(self.driving_seconds), 3),
            "drivingDistance": round(float(self.driving_distance), 3),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Pop":
        job_id = payload.get("jobId")
        return cls(
            id=str(payload["id"]),
            residence_id=str(payload["residenceId"]),
            job_id=None if job_id is None else str(job_id),
            size=float(payload["size"]),
            driving_seconds=float(payload["drivingSeconds"]),
            driving_distance=float(payload["drivingDistance"]),
        )


@dataclass(frozen=True, slots=True)
class DemandRecord:
    """Точка спроса в формате пакета города (§A.5f)."""

    id: str
    lon: float
    lat: float
    jobs: float
    residents: float
    pop_ids: tuple[str, ...] = ()

    @property
    def location(self) -> list[float]:
        """``[долгота, широта]`` — порядок задан документом и не переставляется."""
        return [float(self.lon), float(self.lat)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "location": self.location,
            "jobs": float(self.jobs),
            "residents": float(self.residents),
            "popIds": list(self.pop_ids),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DemandRecord":
        location = payload["location"]
        return cls(
            id=str(payload["id"]),
            lon=float(location[0]),
            lat=float(location[1]),
            jobs=float(payload["jobs"]),
            residents=float(payload["residents"]),
            pop_ids=tuple(str(value) for value in payload.get("popIds", ())),
        )

    @classmethod
    def from_point(cls, point: DemandPoint) -> "DemandRecord":
        return cls(
            id=str(point.key),
            lon=float(point.lon),
            lat=float(point.lat),
            jobs=float(point.jobs),
            residents=float(point.residents),
        )


def _key_of(value: Any) -> str:
    """Ключ точки из строки или из объекта с полем ``key``.

    Строка и объект — два вида одного и того же, и ``str(obj)`` для второго
    даёт **repr** dataclass'а, а не ключ. Подмена выглядит правдоподобно: файл
    собирается, точки называются мусором, и ошибка обнаруживается только в
    `demand_residents_match`, то есть в самом конце. Поэтому вид определяется
    явно, а не тем, что ``str()`` сегодня вернёт.
    """
    if isinstance(value, str):
        return value
    key = getattr(value, "key", None)
    if key is not None:
        return str(key)
    raise TypeError(
        f"в паре OD ожидался ключ точки или объект с полем key, получено "
        f"{type(value).__name__}"
    )


@dataclass(frozen=True, slots=True)
class DemandData:
    """Файл данных спроса целиком: ``{"points": …, "pops": …}``."""

    points: tuple[DemandRecord, ...]
    pops: tuple[Pop, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "points": [point.to_dict() for point in self.points],
            "pops": [pop.to_dict() for pop in self.pops],
        }

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            indent=indent,
            sort_keys=True,
            separators=(",", ":") if indent is None else None,
            allow_nan=False,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DemandData":
        return cls(
            points=tuple(
                DemandRecord.from_dict(item) for item in payload.get("points", ())
            ),
            pops=tuple(Pop.from_dict(item) for item in payload.get("pops", ())),
        )

    @classmethod
    def from_json(cls, text: str) -> "DemandData":
        return cls.from_dict(json.loads(text))

    # ── Инварианты (§18.5, проверка demand_residents_match) ───────────

    def residents_from_pops(self) -> dict[str, float]:
        """Сколько людей насчитали попы, по каждой точке проживания."""
        totals: dict[str, float] = {}
        for pop in self.pops:
            totals[pop.residence_id] = (
                totals.get(pop.residence_id, 0.0) + float(pop.size)
            )
        return totals

    def residents_mismatch(self) -> dict[str, float]:
        """Расхождения ``residents`` против суммы размеров попов.

        Пустой результат означает выполненный инвариант. Ключ — точка, значение
        — модуль расхождения в людях.
        """
        from_pops = self.residents_from_pops()
        mismatches: dict[str, float] = {}
        for point in self.points:
            declared = float(point.residents)
            counted = float(from_pops.get(point.id, 0.0))
            if abs(declared - counted) > 0.5:
                mismatches[point.id] = declared - counted
        return mismatches

    def phantom_points(self) -> tuple[str, ...]:
        """Точки, у которых нет ни одного пассажира (§18.5).

        Точка без жителей **и** без рабочих мест — не точка спроса, а артефакт
        геометрии. Точка с жителями, но без попов — уже нарушение инварианта
        выше; здесь ловится только полная пустота.
        """
        return tuple(
            point.id
            for point in self.points
            if float(point.residents) <= 0.0 and float(point.jobs) <= 0.0
        )

    def unreferenced_pops(self) -> tuple[str, ...]:
        """Попы, ссылающиеся на несуществующую точку.

        Поп с ``jobId = None`` сюда **не** попадает: назначение не достижимо —
        это состояние города, а не потерянная ссылка, и оно считается отдельно
        через :meth:`destinations_unreachable`.
        """
        known = {point.id for point in self.points}
        return tuple(
            pop.id
            for pop in self.pops
            if pop.residence_id not in known
            or (pop.job_id is not None and pop.job_id not in known)
        )

    def destinations_unreachable(self) -> tuple[str, ...]:
        """Попы, у которых нет достижимого назначения.

        Начало, все назначения которого дальше порога притяжения (§5.5).
        Поездки существуют, а назначения у них нет — и это обязано быть видно
        в отчёте, а не тонуть в сумме размеров.
        """
        return tuple(pop.id for pop in self.pops if pop.job_id is None)

    def pop_size_histogram(self) -> dict[str, int]:
        """Распределение размеров попов **в корзинах §A.5f**.

        Корзины взяты из документа (1–9, 10–49, 50–99, 100–199, 200+), а не
        придуманы: иначе распределение не с чем сравнить, а сравнивать есть с
        чем — поставляемый город разобран по этим же корзинам.
        """
        buckets = ("1-9", "10-49", "50-99", "100-199", "200+")
        histogram: dict[str, int] = {}
        for pop in self.pops:
            size = int(pop.size)
            if size >= 200:
                bucket = "200+"
            elif size >= 100:
                bucket = "100-199"
            elif size >= 50:
                bucket = "50-99"
            elif size >= 10:
                bucket = "10-49"
            else:
                bucket = "1-9"
            histogram[bucket] = histogram.get(bucket, 0) + 1
        # Все корзины присутствуют, даже пустые: отсутствие корзины и ноль в ней
        # — разные вещи в отчёте.
        return {bucket: histogram.get(bucket, 0) for bucket in buckets}


def build_pops(
    points: Sequence[DemandPoint],
    pairs: Iterable[tuple[Any, Any, float, float, float]],
    total_residents: float,
    norm: float,
    *,
    max_pop_size: int = MAX_POP_SIZE,
    car_speed_kmh: float = CAR_FREE_SPEED_KMH,
) -> DemandData:
    """Генерирует файл данных спроса из точек и OD-пары (§5.5, §A.5f).

    :param pairs: пары ``(начало, конец, вес, расстояние_м, γ)``. Начало и конец
        принимаются и ключом-строкой, и объектом с полем ``key``.
    :param total_residents: **население**, а не число поездок.

    Различие существенно, и путать его нельзя. Поп — это группа **людей**,
    которые едут из дома на работу; каждый человек входит ровно в одну такую
    группу. Поэтому сумма размеров попов равна населению, и именно это
    утверждает проверка ``demand_residents_match``. Число поездок за день — другое
    число (§32.6 считает ``trips_per_person``), и подставить его сюда означало
    бы получить файл, где людей вдвое больше, чем живёт в городе.

    Разбиение на попы идёт **детерминированно** (§32.5): пары сортируются по
    ключам, и порядок разбиения не зависит от порядка во входе. Идентификаторы
    попов строятся из ключей пары и номера части, а не из счётчика по порядку
    обхода, — иначе перестановка точек переименовала бы половину города.
    """
    if total_residents <= 0.0:
        raise ValueError("население должно быть положительным")

    records = tuple(
        sorted(
            (DemandRecord.from_point(point) for point in points),
            key=lambda record: record.id,
        )
    )

    speed_mps = max(1.0, float(car_speed_kmh) * 1000.0 / 3600.0)
    ordered_pairs = sorted(
        ((_key_of(origin), _key_of(dest), float(weight), float(dist), gamma)
         for origin, dest, weight, dist, gamma in pairs),
        key=lambda item: (item[0], item[1], item[3]),
    )

    # Ограничение по строке: сумма попов каждого начала равна его населению.
    #
    # Без него инвариант `demand_residents_match` не может выполниться. При
    # сквозной нормировке по OD-матрице веса разных начал суммируются в разные
    # величины, и доля поездок у точки оказывается пропорциональна её весу, а не
    # её населению: на двух точках в 1 000 и 800 человек расхождение доходило
    # до ±270 человек. Документ выбирает по этому поводу двусторонне
    # ограниченную
    # модель (§18.5) именно потому, что она «воспроизводит измеренное число
    # приезжающих и уезжающих в каждом районе **точно**».
    #
    # Здесь реализовано ограничение по строке. Ограничение по столбцу (рабочие
    # места) требует измеренных итогов по назначениям; без них процедура
    # вырождается, и подгонять числа под отсутствующие измерения — значит
    # выдумать измерение (§27.1, правило 8).
    residents_by_origin: dict[str, float] = {}
    for point in points:
        if float(point.residents) > 0.0:
            residents_by_origin[str(point.key)] = float(point.residents)

    declared = sum(residents_by_origin.values())
    if abs(declared - float(total_residents)) > max(1.0, 0.001 * declared):
        raise ValueError(
            f"total_residents={total_residents:,.0f} не совпадает с суммой "
            f"residents по точкам {declared:,.0f}. Разные числа означают, что "
            "файл данных спроса собирается не из тех точек, которые приведены, "
            "и инвариант demand_residents_match не выполнится."
        )
    if norm < 0.0:
        raise ValueError(
            "нормировка OD-матрицы отрицательна: сумма весов не может быть "
            "отрицательной величиной"
        )

    weight_sums: dict[str, float] = {}
    for residence, _destination, weight, _dist, _gamma in ordered_pairs:
        weight_sums[residence] = weight_sums.get(residence, 0.0) + weight

    pops: list[Pop] = []
    by_point: dict[str, list[str]] = {}
    largest_by_origin: dict[str, int] = {}
    for residence, destination, weight, dist_m, _gamma in ordered_pairs:
        target = residents_by_origin.get(residence, 0.0)
        row_sum = weight_sums.get(residence, 0.0)
        if target <= 0.0 or row_sum <= 0.0:
            continue
        flow = target * (weight / row_sum)
        if not math.isfinite(flow) or flow <= 0.0:
            continue
        remaining = flow
        part = 0
        while remaining >= 0.5:
            size = min(float(max_pop_size), remaining)
            pop_id = f"{residence}->{destination}#{part}"
            pops.append(
                Pop(
                    id=pop_id,
                    residence_id=residence,
                    job_id=destination,
                    size=size,
                    driving_seconds=float(dist_m) / speed_mps,
                    driving_distance=float(dist_m),
                )
            )
            by_point.setdefault(residence, []).append(pop_id)
            by_point.setdefault(destination, []).append(pop_id)
            previous = largest_by_origin.get(residence)
            if previous is None or size > pops[previous].size:
                largest_by_origin[residence] = len(pops) - 1
            remaining -= size
            part += 1

    # Остаток каждого начала возвращается в его самый большой поп.
    #
    # Минимальный размер попа — один человек (§A.5f), и потоки меньше одного
    # человека не могут стать попом. Но выбросить их молча нельзя: на городе с
    # тысячами назначений на точку таких остатков набираются сотни тысяч
    # человек, и сумма размеров попов перестаёт равняться населению — то есть
    # ломается ровно тот инвариант, который проверяет `demand_residents_match`.
    # Поэтому остаток добавляется в самый большой поп начала: сумма становится
    # точной, а потерянной оказывается только **атрибуция** пузырей меньше
    # человека по конкретному назначению, что при целочисленном размере попов
    # неизбежно.
    for residence, residents in residents_by_origin.items():
        counted = sum(
            pop.size for pop in pops if pop.residence_id == residence
        )
        residual = residents - counted
        if abs(residual) <= 0.5:
            continue
        index = largest_by_origin.get(residence)
        if index is None:
            # У начала нет ни одного попа: все его назначения оказались
            # дальше порога притяжения. Поездки существуют, а попов нет.
            # Одна точка без назначения лучше, чем потерянное население.
            pop_id = f"{residence}->__no_reachable_destination__#0"
            pops.append(
                Pop(
                    id=pop_id,
                    residence_id=residence,
                    job_id=None,
                    size=residents,
                    driving_seconds=0.0,
                    driving_distance=0.0,
                )
            )
            by_point.setdefault(residence, []).append(pop_id)
            continue
        current = pops[index]
        pops[index] = Pop(
            id=current.id,
            residence_id=current.residence_id,
            job_id=current.job_id,
            size=current.size + residual,
            driving_seconds=current.driving_seconds,
            driving_distance=current.driving_distance,
        )

    with_pop_ids = tuple(
        DemandRecord(
            id=record.id,
            lon=record.lon,
            lat=record.lat,
            jobs=record.jobs,
            residents=record.residents,
            pop_ids=tuple(sorted(by_point.get(record.id, ()))),
        )
        for record in records
    )
    return DemandData(
        points=with_pop_ids,
        pops=tuple(pops),
    )


def to_demand_points(data: DemandData) -> list[DemandPoint]:
    """Файл данных спроса обратно в точки пассажирского расчёта.

    Ключ точки — её ``id``: расчёт сопоставляет точки по ключу, и подмена его на
    индекс молча переставила бы места жительства и места работы.
    """
    return [
        DemandPoint(
            key=record.id,
            lat=record.lat,
            lon=record.lon,
            residents=record.residents,
            jobs=record.jobs,
        )
        for record in data.points
    ]


__all__ = [
    "DEMAND_DATA_FILENAME",
    "DemandData",
    "DemandRecord",
    "Pop",
    "build_pops",
    "to_demand_points",
]
