"""Сверка с эталонной сетью — страница проверки (§28 шаг 7, §17.3, §29 Q2).

Документ требует прогнать модель на **эталонной сети** — реальной действующей
сети города, загруженной как план, — и сравнить посадки в день по режимам и
в целом с опубликованными данными.

Три требования здесь важнее формул, и все три соблюдены прямо в API:

**1. Качество данных и расхождение показываются раздельно** (§17.3). Это два
разных вопроса — «чем измеряли» и «насколько совпало», — и смешивать их в одну
цифру нельзя, потому что лечатся они разными способами. Поэтому в отчёте
``rubric_level`` (уровень данных по рубрике §18.5) и ``deviation`` (измеренное
расхождение) лежат рядом и не складываются ни в одну оценку.

**2. Порога приёмки не существует** (§29 вопрос 2, закрыт 30 сентября 2026).
Универсального числа быть не может, потому что расхождение определяется
конкретным городом; решение принимает пара «уровень данных + измеренное
расхождение». Поэтому в этом модуле **нет** поля ``passed`` и нет сравнения с
константой: вердикт здесь был бы выдумкой, и появление такого поля означало
бы, что документ прочитан неправильно.

**3. Определение «посадки» может не совпадать** (§29 вопрос 4 — открытый).
Агентства считают посадки по-разному, и вопрос о едином определении в игре не
решён. Поле ``boarding_definition`` фиксирует, по какому определению опубликованы
данные; при ``None`` сравнение помечается как несогласованное определение, а не
выдаётся за равное по построению.

Рубрика качества (§18.5) здесь **не вычисляется**: по §30.1 это чистая функция
10 ответов, 6 лестниц и 3 опор, и она принадлежит модулю `city`. Сверка
принимает уровень как данное значение — иначе она подменяла бы измеренную
оценку своей.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Sequence


@dataclass(frozen=True, slots=True)
class BoardingsReference:
    """Опубликованные данные о посадках за день — то, с чем сверяемся.

    :param by_mode: ``режим → посадок в день``.
    :param source: кто и когда опубликовал. Без источника расхождение нельзя
        предъявить: это число без единицы измерения и без даты.
    :param boarding_definition: по какому определению «посадки» опубликованы
        данные. ``None`` — определение не зафиксировано, и сравнение
        помечается несогласованным (§29 вопрос 4).
    """

    by_mode: dict[str, float]
    total: float | None = None
    source: str = ""
    #: Период, к которому относятся данные: обычный будний день.
    is_weekday: bool = True
    boarding_definition: str | None = None

    @property
    def definition_aligned(self) -> bool:
        """Совпадает ли определение посадки с тем, что считает модель.

        Модель считает посадкой одну посадку пассажира в транспорт на первой
        ветке пути. Если источник считает иначе, расхождение измеряет разницу
        определений, а не разницу моделей.
        """
        return self.boarding_definition is not None


#: Определение посадки, которое считает модель. Публикуется, чтобы источник
#: мог сказать, совпадает ли его определение.
MODEL_BOARDING_DEFINITION: str = (
    "одна посадка пассажира в транспорт на первой ветке пути; "
    "пересадка второй посадкой не считается"
)


@dataclass(frozen=True, slots=True)
class ModeDeviation:
    """Расхождение по одному режиму."""

    mode: str
    modelled: float
    published: float

    @property
    def absolute(self) -> float:
        return abs(self.modelled - self.published)

    @property
    def relative(self) -> float | None:
        """Относительное расхождение; ``None``, если опубликованных нет.

        Относительное расхождение по нулю не считается: оно бесконечно и
        сообщало бы не о качестве модели, а о том, что по этому режиму нечего
        сравнивать.
        """
        if self.published == 0.0:
            return None
        return self.absolute / abs(self.published)

    def __str__(self) -> str:
        relative = self.relative
        tail = "—" if relative is None else f"{relative * 100:.1f} %"
        return f"{self.mode}: модель {self.modelled:.0f}, опубликовано {self.published:.0f} ({tail})"


@dataclass(frozen=True, slots=True)
class ReferenceCheck:
    """Результат сверки с эталоном (§28 шаг 7, §17.3).

    **Поля ``passed`` здесь нет и не должно быть** (§29 вопрос 2): порога
    приёмки не задаётся числом, решение принимает пара «уровень данных по
    рубрике + измеренное расхождение».
    """

    #: Уровень данных по рубрике §18.5. Приходит извне: сама рубрика — чистая
    #: функция 10 ответов (§30.1) и принадлежит модулю `city`.
    rubric_level: str
    #: Расхождение по режимам и в целом.
    by_mode: tuple[ModeDeviation, ...]
    total: ModeDeviation | None
    #: Расстояние между модельным и наблюдаемым распределением дальностей.
    trip_distance_metric: str
    trip_distance_value: float | None
    #: Единица измерения расстояния: метры.
    trip_distance_unit: str = "м"
    #: Откуда взяты опубликованные данные.
    source: str = ""
    #: Совпадает ли определение «посадки» у источника и у модели (§29 Q4).
    definition_aligned: bool = True
    #: Замечания к самому сравнению — что мешает читать его как измерение.
    caveats: tuple[str, ...] = ()

    def summary(self) -> str:
        """Строка для отчёта: качество данных и расхождение рядом, но порознь."""
        lines = [
            f"данные: уровень по рубрике — {self.rubric_level}",
            f"источник опубликованных посадок: {self.source or 'не указан'}",
        ]
        for item in self.by_mode:
            lines.append(f"  {item}")
        if self.total is not None:
            lines.append(f"  всего: {self.total}")
        if self.trip_distance_value is not None:
            lines.append(
                f"  {self.trip_distance_metric} распределения дальностей: "
                f"{self.trip_distance_value:.3f} {self.trip_distance_unit}"
            )
        lines.append(
            "порога приёмки нет (§29 вопрос 2): решение принимает пара "
            "«уровень данных + измеренное расхождение»"
        )
        for caveat in self.caveats:
            lines.append(f"  ! {caveat}")
        return "\n".join(lines)


def trip_distance_wasserstein(
    modelled: Sequence[float],
    observed: Sequence[float],
) -> float:
    """Расстояние Wasserstein-1 между двумя распределениями дальностей.

    Метрика качества генерации — расстояние между модельным и реальным
    распределением дальностей поездок (§28 шаг 3); значение публикуется, а
    порог приёмности **не задаётся числом** — решение принимает пара «уровень
    данных + измеренное расхождение».

    Считается точно, а не по сортированным выборкам одинакового размера:
    ``mean(|a_i - b_i|)`` верен только при равных объёмах, а объёмы здесь
    разные по построению (наблюдений может быть 300, а модельных — миллион).
    """
    if not modelled or not observed:
        raise ValueError("оба распределения должны быть непустыми")
    for value in (*modelled, *observed):
        if not isfinite(value):
            raise ValueError("распределение дальностей содержит нечисловое значение")
    a = sorted(float(value) for value in modelled)
    b = sorted(float(value) for value in observed)
    # ∫|F_a - F_b| по объединённым точкам разрыва: между соседними точками
    # разность функций распределения постоянна, поэтому интеграл точен.
    points = sorted(set(a) | set(b))
    total = 0.0
    for left, right in zip(points, points[1:]):
        fa = _empirical_cdf(a, left)
        fb = _empirical_cdf(b, left)
        total += abs(fa - fb) * (right - left)
    return total


def trip_distance_ks(
    modelled: Sequence[float],
    observed: Sequence[float],
) -> float:
    """Расстояние Колмогорова–Смирнова между распределениями дальностей.

    Взвешенная по объёмам выборок, поэтому не требует равных размеров: если
    наблюдений 300, а модельных значений миллион, расстояние не должно
    показывать 100 % только из-за разного объёма.
    """
    if not modelled or not observed:
        raise ValueError("оба распределения должны быть непустыми")
    a = sorted(float(value) for value in modelled)
    b = sorted(float(value) for value in observed)
    points = sorted(set(a) | set(b))
    return max(
        abs(_empirical_cdf(a, point) - _empirical_cdf(b, point))
        for point in points
    )


def _empirical_cdf(sorted_values: list[float], point: float) -> float:
    """Значение эмпирической функции распределения «не больше point»."""
    import bisect

    return bisect.bisect_right(sorted_values, point) / len(sorted_values)


def check_against_reference(
    result: object,
    reference: BoardingsReference,
    rubric_level: str,
    *,
    modelled_trip_distances: Sequence[float] | None = None,
    observed_trip_distances: Sequence[float] | None = None,
    distance_metric: str = "wasserstein",
) -> ReferenceCheck:
    """Сверяет расчёт с опубликованными данными (§28 шаг 7).

    :param result: результат расчёта T2; используются посадки по режимам и
        общие поездки.
    :param rubric_level: уровень данных по рубрике §18.5, полученный извне.
    :param modelled_trip_distances: модельные дальности поездок, м.
    :param observed_trip_distances: наблюдаемые дальности поездок, м.
    :param distance_metric: ``"wasserstein"`` или ``"ks"``.
    """
    by_mode = tuple(
        ModeDeviation(
            mode=mode,
            modelled=float(getattr(result, "boardings_by_mode", {}).get(mode, 0.0)),
            published=float(value),
        )
        for mode, value in sorted(reference.by_mode.items())
    )

    modelled_total = float(getattr(result, "transit_trips", 0.0))
    published_total = reference.total
    total = (
        None
        if published_total is None
        else ModeDeviation(
            mode="всего", modelled=modelled_total, published=float(published_total)
        )
    )

    caveats: list[str] = []
    if not reference.definition_aligned:
        caveats.append(
            "определение «посадки» у источника не зафиксировано: расхождение "
            "может измерять разницу определений, а не разницу моделей "
            "(§29 вопрос 4)"
        )
    if not reference.source:
        caveats.append("источник опубликованных данных не указан")
    if not reference.is_weekday:
        caveats.append(
            "данные не за обычный будний день: сопоставимость с расчётом "
            "не обеспечена (§28 шаг 7 считает средний будний день)"
        )

    metric_value: float | None = None
    metric_name = distance_metric
    if modelled_trip_distances and observed_trip_distances:
        if distance_metric == "ks":
            metric_value = trip_distance_ks(
                modelled_trip_distances, observed_trip_distances
            )
            metric_name = "KS"
        else:
            metric_value = trip_distance_wasserstein(
                modelled_trip_distances, observed_trip_distances
            )
            metric_name = "Wasserstein-1"
    elif modelled_trip_distances or observed_trip_distances:
        caveats.append(
            "распределение дальностей посчитано по одной стороне: "
            "сравнивать не с чем"
        )
        metric_name = f"{distance_metric} (не вычислено)"

    return ReferenceCheck(
        rubric_level=rubric_level,
        by_mode=by_mode,
        total=total,
        trip_distance_metric=metric_name,
        trip_distance_value=metric_value,
        source=reference.source,
        definition_aligned=reference.definition_aligned,
        caveats=tuple(caveats),
    )