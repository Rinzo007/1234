"""Рубрика качества данных города: шесть лестниц, три опоры, семь уровней.

§18.5 задаёт рубрику полностью, включая формулу. Здесь она реализована по
спецификации, а не по смыслу: веса, ступени и пороги взяты из документа, и
формула проверена на независимом примере из него же — городе Аахен, где
документ приводит и ответы, и результат («confirmed low 0.41»).

## Почему это не «уровень качества» одной строкой

Трёхзначного поля не хватает принципиально: оно не различает «много рабочих
мест не по местам» и «мало рабочих мест, но точно». Рубрика отвечает на шесть
разных вопросов и сводит их к трём опорам с числами, которые можно
воспроизвести.

## Две оценки, и это не избыточность

| Оценка | Формула | Кому |
|---|---|---|
| ``raw_score`` | произведение блоков × G — «слабое звено» | сопровождающему, строгая |
| ``weighted_score`` | среднее блоков × G — половина и половина | игроку, из неё уровень |

Произведение не даёт компенсировать плохую детализацию хорошим счётом: город с
идеально измеренными рабочими местами, размазанными равномерно по территории,
получает низкую строгую оценку. Среднее даёт игроку читаемую оценку, не обнуляя
город из-за одной слабой характеристики.

## Ключевое правило, которое чаще всего нарушают

Отвечать надо на вопрос о **месте публикации числа в источнике**, а не о том,
насколько мелко автор разложил готовые точки (§18.5ac). Автор, разложивший
сорок муниципальных итогов по двадцати тысячам зданий, не приобрёл точность:
данные опубликованы на уровне муниципалитета. Поэтому лестница ``G`` отвечает
на вопрос «где опубликовано», лестница ``R`` — на вопрос «насколько мелко
разложили», и это разные ответы на разные вопросы.

## Кто назначает уровень

Уровень **не может быть заявлен автором города**: его вычисляет функция, а
подтверждает сопровождающий (§18.5, «кто назначает уровень»). Модуль вычисляет
и никогда не принимает готового уровня на вход — иначе манифест разошёлся бы с
данными, а это ровно тот класс ошибок, который ловится только в CI.

Версия рубрики закреплена в :data:`RUBRIC_VERSION`. Изменение любого веса или
порога требует её явного увеличения — иначе старый манифест и новый расчёт
разошлись бы незаметно (§27.1, правило 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


#: Версия рубрики. Меняется только вместе с весом, порогом или набором ступеней.
RUBRIC_VERSION: str = "1.0.0"

# ── Три опоры и их веса (§18.5) ───────────────────────────────────────

PILLAR_WEIGHTS: dict[str, float] = {
    "jobs": 0.50,
    "residents": 0.35,
    "od_matrix": 0.15,
}

# ── Шесть лестниц (§18.5) ─────────────────────────────────────────────

#: Лестница 1. Основание счёта рабочих мест.
JOBS_BASIS: dict[str, float] = {
    "physical_measured": 1.00,
    "physical_inferred": 0.85,
    "registered_self_declared": 0.70,
    "size_bands": 0.50,
    "estimated_proxy": 0.30,
    "none": 0.00,
}

#: Лестница 2. Основание счёта населения.
RESIDENTS_BASIS: dict[str, float] = {
    "employed_residents": 1.00,
    "working_age": 0.70,
    "total_population": 0.40,
    "none": 0.00,
}

#: Лестница 3. Пространственное разрешение R — насколько мелко разложили массу.
SPATIAL_RESOLUTION: dict[str, float] = {
    "exact_footprints": 1.00,
    "mesh_125_or_adm5": 0.90,
    "mesh_250": 0.85,
    "mesh_500": 0.75,
    "ml_hybrid_footprints": 0.70,
    "osm_footprints": 0.60,
    "mesh_1km": 0.50,
    "admin_polygon": 0.30,
}

#: Лестница 4. Интенсивность I — как общий итог делится между кандидатами.
INTENSITY: dict[str, float] = {
    "measured_per_unit": 1.00,
    "fine_types_calibrated": 0.85,
    "fine_types_generic": 0.60,
    "coarse_sector": 0.50,
    "binary_split": 0.40,
    "size_only": 0.25,
    "uniform": 0.10,
}

#: Лестница 5. Детализация измерения G — на какой единице число опубликовано.
#:
#: Это **не** то же, что лестница 3. Ответ на вопрос «где опубликовано» (§18.5ac),
#: а не «насколько мелко разложили».
#:
#: В документе лестница набрана двумя колонками; здесь она развёрнута в один
#: список **по убыванию веса**, потому что по двум колонкам нельзя прочитать
#: лестницу как шкалу — а это её единственная работа. Значения те же, включая
#: две пары с одинаковым весом (``mesh_250`` = ``adm5`` = 0,95 и
#: ``mesh_500`` = ``adm4`` = 0,90): разводить их значило бы изобрести различие,
#: которого в источнике нет.
GRANULARITY: dict[str, float] = {
    "mesh_125": 1.00,
    "mesh_250": 0.95,
    "adm5": 0.95,
    "mesh_500": 0.90,
    "adm4": 0.90,
    "mesh_1km": 0.85,
    "adm3": 0.70,
    "mesh_coarse": 0.65,
    "adm2": 0.50,
    "adm1": 0.30,
    "none": 0.00,
}

#: Лестница 6. Метрика связей начало–конец.
OD_METRIC: dict[str, float] = {
    "full_matrix": 1.00,
    "structured_marginals": 0.75,
    "marginal_od": 0.50,
    "synthetic_measured_marginals": 0.25,
    "prior_informed_synthetic": 0.10,
    "none": 0.00,
}

# ── Уровни и пороги (§18.5) ───────────────────────────────────────────

#: Пороги в порядке убывания. ``unknown`` — не порог, а седьмое состояние.
LEVEL_THRESHOLDS: tuple[tuple[str, float], ...] = (
    ("very-high", 0.75),
    ("high", 0.60),
    ("medium", 0.45),
    ("low", 0.30),
    ("very-low", 0.15),
    ("absent", 0.00),
)

#: Седьмой уровень отделён от ``absent`` намеренно: «мы оценили и данных нет» и
#: «мы не смотрели» — разные вещи, и смешивать их нельзя (§27.1, правило 1).
LEVEL_UNKNOWN: str = "unknown"


@dataclass(frozen=True, slots=True)
class PillarScore:
    """Оценка одной опоры: блоки, две оценки и гранулярность."""

    name: str
    #: Значения блоков. У опоры «связи» блок один — лестница 6 одна, и
    #: подставлять в неё чужие ступени нельзя (§18.5).
    blocks: tuple[float, ...]
    granularity: str
    raw: float
    weighted: float

    @property
    def weight(self) -> float:
        return PILLAR_WEIGHTS[self.name]


@dataclass(frozen=True, slots=True)
class RubricResult:
    """Итог рубрики города."""

    pillars: tuple[PillarScore, ...]
    raw_score: float
    weighted_score: float
    level: str
    rubric_version: str = RUBRIC_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "rubric_version": self.rubric_version,
            "raw_score": self.raw_score,
            "weighted_score": self.weighted_score,
            "level": self.level,
            "pillars": {
                pillar.name: {
                    "blocks": list(pillar.blocks),
                    "granularity": pillar.granularity,
                    "raw": pillar.raw,
                    "weighted": pillar.weighted,
                    "weight": pillar.weight,
                }
                for pillar in self.pillars
            },
        }

    def summary(self) -> str:
        parts = ", ".join(
            f"{pillar.name}: raw {pillar.raw:.3f} / weighted {pillar.weighted:.3f}"
            for pillar in self.pillars
        )
        return (
            f"рубрика {self.rubric_version}: raw {self.raw_score:.3f}, "
            f"weighted {self.weighted_score:.3f} → {self.level} ({parts})"
        )


def level_for(weighted_score: float) -> str:
    """Уровень по взвешенной оценке."""
    for name, threshold in LEVEL_THRESHOLDS:
        if weighted_score >= threshold:
            return name
    return LEVEL_UNKNOWN


def _lookup(ladder: dict[str, float], value: str | None, context: str) -> float:
    """Ступень лестницы; неизвестное значение — ошибка, а не ноль.

    Ноль был бы незаметной подменой: ступень ``none`` означает «мы оценили и
    данных нет», а неизвестное значение означает «мы не знаем, что это». Разница
    та же, что между ``absent`` и ``unknown`` в уровнях.
    """
    if value is None:
        raise ValueError(
            f"{context}: ответ не задан. Отсутствие ответа — это "
            f"«не оценивали» (уровень {LEVEL_UNKNOWN}), и оно не равно нулю."
        )
    if value not in ladder:
        raise ValueError(
            f"{context}: ступень {value!r} не входит в лестницу; допустимы: "
            + ", ".join(sorted(ladder))
        )
    return ladder[value]


def _two_block_pillar(
    name: str,
    basis_ladder: dict[str, float],
    basis: str | None,
    resolution: str | None,
    intensity: str | None,
    granularity: str | None,
) -> PillarScore:
    """Опора из двух блоков: ``[основание]`` и ``[R · I]``.

    R и I — не два блока, а один: это две ступени одного вопроса «куда может
    попасть масса», и в исходной формуле они входят множителем как одно целое.
    Разведённые по блокам, ошибка разрешения начала бы «отыгрываться» ошибкой
    интенсивности, а среднее изменило бы смысл.
    """
    basis_score = _lookup(basis_ladder, basis, f"{name}: основание счёта")
    resolution_score = _lookup(
        SPATIAL_RESOLUTION, resolution, f"{name}: разрешение"
    )
    intensity_score = _lookup(INTENSITY, intensity, f"{name}: интенсивность")
    granularity_score = _lookup(GRANULARITY, granularity, f"{name}: гранулярность")

    second = resolution_score * intensity_score
    blocks = (basis_score, second)
    raw = 1.0
    for block in blocks:
        raw *= block
    raw *= granularity_score
    weighted = (sum(blocks) / len(blocks)) * granularity_score
    return PillarScore(
        name=name,
        blocks=blocks,
        granularity=str(granularity),
        raw=raw,
        weighted=weighted,
    )


def _od_pillar(basis: str | None, granularity: str | None) -> PillarScore:
    """Опора «Матрица поездок»: один блок, поэтому обе оценки совпадают."""
    basis_score = _lookup(OD_METRIC, basis, "od_matrix: метрика связей")
    granularity_score = _lookup(GRANULARITY, granularity, "od_matrix: гранулярность")
    return PillarScore(
        name="od_matrix",
        blocks=(basis_score,),
        granularity=str(granularity),
        raw=basis_score * granularity_score,
        weighted=basis_score * granularity_score,
    )


def evaluate_rubric(
    *,
    jobs_basis: str | None = None,
    jobs_resolution: str | None = None,
    jobs_intensity: str | None = None,
    jobs_granularity: str | None = None,
    residents_basis: str | None = None,
    residents_resolution: str | None = None,
    residents_intensity: str | None = None,
    residents_granularity: str | None = None,
    od_metric: str | None = None,
    od_granularity: str | None = None,
) -> RubricResult:
    """Вычисляет рубрику города по шести ответам (плюс гранулярности трёх).

    Готовый уровень на вход **не принимается**: уровень назначается этой
    функцией, а подтверждает его сопровождающий (§18.5). Манифест, заявивший
    свой уровень, расходился бы с данными — и расхождение обнаруживалось бы
    только в CI.
    """
    pillars = (
        _two_block_pillar(
            "jobs", JOBS_BASIS, jobs_basis,
            jobs_resolution, jobs_intensity, jobs_granularity,
        ),
        _two_block_pillar(
            "residents", RESIDENTS_BASIS, residents_basis,
            residents_resolution, residents_intensity, residents_granularity,
        ),
        _od_pillar(od_metric, od_granularity),
    )

    # Веса опор применяются к обеим оценкам. Это видно на примере Аахена:
    # ``0,50·0,143 + 0,35·0,168 + 0,15·0,525 = 0,209`` — та же формула, что и
    # для ``weighted``, применённая к «строгим» значениям опор. Простое сложение
    # дало бы 0,836, и «строгая» оценка оказалась бы завышенной вчетверо.
    raw_score = sum(pillar.raw * pillar.weight for pillar in pillars)
    weighted_score = sum(
        pillar.weighted * pillar.weight for pillar in pillars
    )
    return RubricResult(
        pillars=pillars,
        raw_score=raw_score,
        weighted_score=weighted_score,
        level=level_for(weighted_score),
    )


#: Ответы города Аахен из документа (§18.5, «проверка на реальном городе»).
#: Приводятся здесь как эталон: реализация обязана воспроизвести документ, и
#: расхождение с этим примером — провал теста, а не «уточнение».
AACHEN_ANSWERS: dict[str, str] = {
    "jobs_basis": "physical_inferred",
    "jobs_resolution": "osm_footprints",
    "jobs_intensity": "binary_split",
    "jobs_granularity": "adm3",
    "residents_basis": "total_population",
    "residents_resolution": "ml_hybrid_footprints",
    "residents_intensity": "fine_types_generic",
    "residents_granularity": "mesh_125",
    "od_metric": "structured_marginals",
    "od_granularity": "adm3",
}

#: Ожидаемый результат для :data:`AACHEN_ANSWERS` по тексту документа.
AACHEN_EXPECTED_RAW: float = 0.209
AACHEN_EXPECTED_WEIGHTED: float = 0.413
AACHEN_EXPECTED_LEVEL: str = "low"


def aachen() -> RubricResult:
    """Рубрика Аахена — сверка реализации с документом."""
    return evaluate_rubric(**AACHEN_ANSWERS)


__all__ = [
    "AACHEN_ANSWERS",
    "AACHEN_EXPECTED_LEVEL",
    "AACHEN_EXPECTED_RAW",
    "AACHEN_EXPECTED_WEIGHTED",
    "GRANULARITY",
    "INTENSITY",
    "JOBS_BASIS",
    "LEVEL_THRESHOLDS",
    "LEVEL_UNKNOWN",
    "OD_METRIC",
    "PILLAR_WEIGHTS",
    "PillarScore",
    "RESIDENTS_BASIS",
    "RUBRIC_VERSION",
    "RubricResult",
    "SPATIAL_RESOLUTION",
    "aachen",
    "evaluate_rubric",
    "level_for",
]
