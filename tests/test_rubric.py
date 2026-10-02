"""Тесты рубрики качества данных (``wikiroutes.rubric``).

Главный тест здесь — воспроизведение примера из документа. Рубрика обязана
давать те же числа, что §18.5 приводит для города Аахен: расхождение здесь
означало бы, что либо веса, либо ступени, либо формула прочитаны неправильно.
"""

from __future__ import annotations

import math

import pytest

import rubric as rb


# ── Воспроизведение примера из документа ──────────────────────────────


def test_rubric_reproduces_the_aachen_example_from_the_document():
    """§18.5: Аахен — raw 0,209, weighted 0,413, уровень ``low``."""
    result = rb.aachen()

    assert result.raw_score == pytest.approx(rb.AACHEN_EXPECTED_RAW, abs=5e-4)
    assert result.weighted_score == pytest.approx(
        rb.AACHEN_EXPECTED_WEIGHTED, abs=5e-4
    )
    assert result.level == rb.AACHEN_EXPECTED_LEVEL


def test_aachen_pillar_values_match_the_document_table():
    """Каждая опора по отдельности: raw 0,143 / 0,168 / 0,525.

    Документ приводит значения с округлением до трёх знаков, поэтому и
    сравнение с допуском в тысячную.
    """
    result = rb.aachen()
    by_name = {pillar.name: pillar for pillar in result.pillars}

    assert by_name["jobs"].raw == pytest.approx(0.143, abs=1e-3)
    assert by_name["jobs"].weighted == pytest.approx(0.382, abs=1e-3)
    assert by_name["residents"].raw == pytest.approx(0.168, abs=1e-3)
    assert by_name["residents"].weighted == pytest.approx(0.410, abs=1e-3)
    # У опоры «связи» блок один, поэтому обе оценки совпадают — это следствие
    # общей формулы, а не исключение из неё.
    assert by_name["od_matrix"].raw == pytest.approx(0.525, abs=1e-3)
    assert by_name["od_matrix"].raw == by_name["od_matrix"].weighted


def test_pillar_weights_are_applied_to_both_scores():
    """Веса опор применяются к raw и к weighted одинаково.

    Без весов у raw сумма выходила бы 0,836 вместо 0,209 — «строгая» оценка
    оказалась бы завышенной вчетверо.
    """
    result = rb.aachen()
    manual_raw = sum(
        pillar.raw * pillar.weight for pillar in result.pillars
    )
    assert result.raw_score == pytest.approx(manual_raw)
    assert result.raw_score < sum(
        pillar.raw for pillar in result.pillars
    )


def test_pillar_weights_sum_to_one():
    assert math.isclose(sum(rb.PILLAR_WEIGHTS.values()), 1.0, abs_tol=1e-12)
    assert rb.PILLAR_WEIGHTS["jobs"] == 0.50
    assert rb.PILLAR_WEIGHTS["residents"] == 0.35
    assert rb.PILLAR_WEIGHTS["od_matrix"] == 0.15


# ── Уровни ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("score", "level"),
    [
        (0.90, "very-high"),
        (0.75, "very-high"),
        (0.74, "high"),
        (0.60, "high"),
        (0.59, "medium"),
        (0.45, "medium"),
        (0.44, "low"),
        (0.30, "low"),
        (0.29, "very-low"),
        (0.15, "very-low"),
        (0.14, "absent"),
        (0.00, "absent"),
    ],
)
def test_level_thresholds(score, level):
    assert rb.level_for(score) == level


def test_unknown_is_not_absent():
    """«Оценили и данных нет» и «не оценивали» — разные вещи (§27.1)."""
    assert rb.level_for(0.0) == "absent"
    assert rb.LEVEL_UNKNOWN == "unknown"
    assert rb.LEVEL_UNKNOWN not in dict(rb.LEVEL_THRESHOLDS)


def test_unknown_cannot_be_reached_by_a_score():
    """``unknown`` — состояние, а не результат вычисления.

    ``absent`` определён как «≥ 0», поэтому отрицательная оценка вне
    определённой области, и честный ответ на неё — ``unknown``.
    """
    assert rb.level_for(-0.01) == rb.LEVEL_UNKNOWN


# ── Формула ───────────────────────────────────────────────────────────


def _perfect_answers() -> dict[str, str]:
    return {
        "jobs_basis": "physical_measured",
        "jobs_resolution": "exact_footprints",
        "jobs_intensity": "measured_per_unit",
        "jobs_granularity": "mesh_125",
        "residents_basis": "employed_residents",
        "residents_resolution": "exact_footprints",
        "residents_intensity": "measured_per_unit",
        "residents_granularity": "mesh_125",
        "od_metric": "full_matrix",
        "od_granularity": "mesh_125",
    }


def test_perfect_city_reaches_very_high():
    result = rb.evaluate_rubric(**_perfect_answers())
    assert result.raw_score == pytest.approx(1.0, abs=1e-9)
    assert result.weighted_score == pytest.approx(1.0, abs=1e-9)
    assert result.level == "very-high"


def test_no_data_city_is_absent_not_zero_weighted():
    """Все ответы ``none`` → ``absent``, а не ``very-low``."""
    result = rb.evaluate_rubric(
        jobs_basis="none", jobs_resolution="admin_polygon",
        jobs_intensity="uniform", jobs_granularity="none",
        residents_basis="none", residents_resolution="admin_polygon",
        residents_intensity="uniform", residents_granularity="none",
        od_metric="none", od_granularity="none",
    )
    assert result.weighted_score == 0.0
    assert result.level == "absent"


def test_raw_is_the_strict_score_not_the_generous_one():
    """Произведение блоков наказывает слабое звено; среднее — прощает.

    Город с отличным основанием счёта и равномерной интенсивностью обязан иметь
    ``raw`` заметно ниже ``weighted`` — это и есть смысл двух оценок.
    """
    result = rb.evaluate_rubric(
        jobs_basis="physical_measured", jobs_resolution="exact_footprints",
        jobs_intensity="uniform", jobs_granularity="mesh_125",
        residents_basis="employed_residents", residents_resolution="exact_footprints",
        residents_intensity="uniform", residents_granularity="mesh_125",
        od_metric="full_matrix", od_granularity="mesh_125",
    )
    assert result.raw_score < result.weighted_score


def test_resolution_and_intensity_form_one_block():
    """R и I — ступени одного вопроса, поэтому один блок ``R · I``.

    Разведённые по блокам, ошибка разрешения отыгрывалась бы ошибкой
    интенсивности, а среднее изменило бы смысл.
    """
    result = rb.evaluate_rubric(
        jobs_basis="physical_inferred", jobs_resolution="osm_footprints",
        jobs_intensity="binary_split", jobs_granularity="adm3",
        residents_basis="none", residents_resolution="admin_polygon",
        residents_intensity="uniform", residents_granularity="none",
        od_metric="none", od_granularity="none",
    )
    jobs = next(p for p in result.pillars if p.name == "jobs")
    assert len(jobs.blocks) == 2
    assert jobs.blocks[1] == pytest.approx(0.60 * 0.40)


def test_od_pillar_has_exactly_one_block():
    result = rb.evaluate_rubric(
        jobs_basis="none", jobs_resolution="admin_polygon",
        jobs_intensity="uniform", jobs_granularity="none",
        residents_basis="none", residents_resolution="admin_polygon",
        residents_intensity="uniform", residents_granularity="none",
        od_metric="structured_marginals", od_granularity="adm3",
    )
    od = next(p for p in result.pillars if p.name == "od_matrix")
    assert len(od.blocks) == 1
    assert od.raw == od.weighted


# ── Защита от молчаливых подмен ───────────────────────────────────────


def test_unknown_rung_is_an_error_not_a_zero():
    """Неизвестная ступень — ошибка. Ноль означал бы «оценили, данных нет»."""
    with pytest.raises(ValueError) as error:
        rb.evaluate_rubric(
            # `total_population` есть в лестнице жителей, но не в лестнице
            # рабочих мест: у опор разные основания счёта.
            jobs_basis="total_population",
            jobs_resolution="exact_footprints", jobs_intensity="measured_per_unit",
            jobs_granularity="mesh_125",
            residents_basis="employed_residents",
            residents_resolution="exact_footprints",
            residents_intensity="measured_per_unit",
            residents_granularity="mesh_125",
            od_metric="full_matrix", od_granularity="mesh_125",
        )
    assert "не входит в лестницу" in str(error.value)


def test_missing_answer_is_an_error_not_a_zero():
    """Отсутствие ответа — это «не оценивали» (unknown), и оно не равно нулю."""
    with pytest.raises(ValueError) as error:
        rb.evaluate_rubric(
            jobs_resolution="exact_footprints", jobs_intensity="measured_per_unit",
            jobs_granularity="mesh_125",
            residents_basis="employed_residents",
            residents_resolution="exact_footprints",
            residents_intensity="measured_per_unit",
            residents_granularity="mesh_125",
            od_metric="full_matrix", od_granularity="mesh_125",
        )
    assert "не задан" in str(error.value)
    assert "unknown" in str(error.value)


def test_granularity_is_answered_by_publication_place_not_by_subdivision():
    """§18.5ac: G — это «где опубликовано», а не «насколько мелко разложили».

    Муниципальные итоги, разложенные по двадцати тысячам зданий, остаются
    опубликованными на уровне муниципалитета: разрешение выросло, гранулярность
    нет. Рубрика обязана это различать.
    """
    coarse_but_fine = rb.evaluate_rubric(
        jobs_basis="registered_self_declared", jobs_resolution="exact_footprints",
        jobs_intensity="measured_per_unit", jobs_granularity="adm3",
        residents_basis="total_population", residents_resolution="exact_footprints",
        residents_intensity="measured_per_unit", residents_granularity="adm3",
        od_metric="none", od_granularity="adm3",
    )
    # Разрешение «идеальное», гранулярность муниципальная — и оценка не идеальная.
    jobs = next(p for p in coarse_but_fine.pillars if p.name == "jobs")
    assert jobs.blocks[1] == pytest.approx(1.0)
    assert jobs.raw == pytest.approx(0.70 * 1.0 * 0.70)
    assert coarse_but_fine.level != "very-high"


# ── Контракт ──────────────────────────────────────────────────────────


def test_rubric_version_is_pinned():
    """Смена веса или порога обязана требовать новой версии (§27.1)."""
    assert rb.RUBRIC_VERSION == "1.0.0"
    result = rb.aachen()
    assert result.rubric_version == rb.RUBRIC_VERSION
    assert result.to_dict()["rubric_version"] == rb.RUBRIC_VERSION


def test_result_is_json_serialisable_and_ordered():
    result = rb.aachen()
    payload = result.to_dict()
    assert list(payload) == [
        "rubric_version", "raw_score", "weighted_score", "level", "pillars",
    ]
    assert set(payload["pillars"]) == {"jobs", "residents", "od_matrix"}
    import json

    assert json.loads(json.dumps(payload)) == payload


def test_every_ladder_is_ordered_and_has_a_best_rung():
    """Каждая лестница упорядочена по убыванию веса и имеет ступень 1,00."""
    for ladder in (
        rb.JOBS_BASIS, rb.RESIDENTS_BASIS, rb.SPATIAL_RESOLUTION,
        rb.INTENSITY, rb.GRANULARITY, rb.OD_METRIC,
    ):
        weights = list(ladder.values())
        assert weights == sorted(weights, reverse=True)
        assert max(weights) == 1.00
        assert len(ladder) >= 4


def test_none_rung_exists_exactly_where_the_document_defines_it():
    """Ступень ``none`` есть не в каждой лестнице — и это по документу.

    В лестнице пространственного разрешения низшая ступень — ``admin_polygon``
    0,30, и нуля там нет: разрешение всегда чем-то ограничено, вопрос только
    чем. Ноль означал бы «разрешения нет», то есть массу некуда класть.
    """
    for ladder in (rb.JOBS_BASIS, rb.RESIDENTS_BASIS, rb.OD_METRIC, rb.GRANULARITY):
        assert "none" in ladder, "ступени none не хватает"
        assert ladder["none"] == 0.0

    assert "none" not in rb.SPATIAL_RESOLUTION
    assert "none" not in rb.INTENSITY
    assert min(rb.SPATIAL_RESOLUTION.values()) == 0.30
    assert min(rb.INTENSITY.values()) == 0.10


def test_granularity_ladder_keeps_the_documents_shared_weights():
    """В лестнице гранулярности две ступени делят вес — так в документе.

    ``mesh_250`` и ``adm5`` обе 0,95; ``mesh_500`` и ``adm4`` обе 0,90.
    Разводить их значило бы изобретать различие, которого в источнике нет.
    """
    assert rb.GRANULARITY["mesh_250"] == rb.GRANULARITY["adm5"] == 0.95
    assert rb.GRANULARITY["mesh_500"] == rb.GRANULARITY["adm4"] == 0.90
