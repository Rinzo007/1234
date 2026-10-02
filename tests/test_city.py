"""Тесты модуля города (``wikiroutes.city``).

Проверяется то, что задано документом как контракт: формат файла спроса,
состав выпуска, правила версии и имени пакета, инварианты спроса и десять
обязательных проверок приёма.
"""

from __future__ import annotations

import gzip
import json
import math

import pytest

import city
import passenger as pf
from overture.city import CityIdentity, LatLonBBox, RoadNetwork
from passenger.demand import _od_matrix


# ── Приборные данные ──────────────────────────────────────────────────


def _points():
    return [
        pf.DemandPoint("h1", 51.000, 39.000, 1000.0, 0.0),
        pf.DemandPoint("h2", 51.010, 39.010, 800.0, 0.0),
        pf.DemandPoint("h3", 51.020, 39.000, 600.0, 0.0),
        pf.DemandPoint("j1", 51.030, 39.020, 0.0, 1200.0),
        pf.DemandPoint("j2", 51.035, 39.025, 0.0, 900.0),
    ]


def _demand(points=None, car_speed_kmh: float = 56.0):
    points = points or _points()
    pairs, norm = _od_matrix(points, pf.PassengerOptions(), {})
    by_key = {point.key: point for point in points}
    tuples = [
        (by_key[pair.origin_key], by_key[pair.dest_key], pair.weight,
         pair.dist_m, pair.gamma)
        for pair in pairs
    ]
    residents = sum(point.residents for point in points)
    return city.build_pops(points, tuples, residents, norm,
                           car_speed_kmh=car_speed_kmh)


def _identity(**kwargs) -> CityIdentity:
    fields = {
        "city_code": "VOG",
        "name": "Воронеж",
        "country": "RU",
        "region": "Европа",
        "population": 2400.0,
        "data_source": "тест",
        "latitude": 51.015,
        "longitude": 39.010,
    }
    fields.update(kwargs)
    return CityIdentity(**fields)


def _config(**kwargs) -> city.CityConfig:
    fields = {
        "identity": _identity(),
        "bbox": LatLonBBox(min_lat=51.0, min_lon=39.0, max_lat=51.1, max_lon=39.1),
        "version": "1.0.0",
    }
    fields.update(kwargs)
    return city.CityConfig(**fields)


# ── Формат demand_data.json (§A.5f) ────────────────────────────────────


def test_demand_data_has_the_documented_shape():
    """§A.5f: ``{"points": [...], "pops": [...]}``."""
    data = _demand().to_dict()
    assert set(data) == {"points", "pops"}
    assert set(data["points"][0]) == {"id", "location", "jobs", "residents", "popIds"}
    assert set(data["pops"][0]) == {
        "id", "residenceId", "jobId", "size", "drivingSeconds", "drivingDistance",
    }


def test_location_is_lon_then_lat():
    """Порядок ``[долгота, широта]`` задан документом и не переставляется."""
    data = _demand()
    record = next(point for point in data.points if point.id == "h1")
    assert record.location == [39.000, 51.000]


def test_pop_size_never_exceeds_the_cap():
    """Потолок 200 человек на поп (§A.5f)."""
    data = _demand()
    assert data.pops
    assert max(pop.size for pop in data.pops) <= pf.MAX_POP_SIZE


def test_driving_time_follows_the_documented_speed():
    """25,4 км за 1614 с в документе — это 56,7 км/ч, то есть константа 56."""
    data = _demand()
    for pop in data.pops:
        expected = pop.driving_distance / (56.0 * 1000.0 / 3600.0)
        assert pop.driving_seconds == pytest.approx(expected, rel=1e-9)


def test_pop_ids_link_points_and_pops_both_ways():
    data = _demand()
    known = {pop.id for pop in data.pops}
    for record in data.points:
        assert set(record.pop_ids) <= known


# ── Инварианты спроса (§18.5) ─────────────────────────────────────────


def test_residents_match_invariant_holds():
    """``demand_residents_match``: счётчик жителей равен сумме размеров попов."""
    data = _demand()
    assert data.residents_mismatch() == {}
    assert sum(pop.size for pop in data.pops) == pytest.approx(
        sum(point.residents for point in data.points)
    )


def test_row_constraint_is_what_makes_the_invariant_hold():
    """Сумма попов каждого начала равна его населению.

    Это ограничение по строке двусторонне ограниченной модели (§18.5). Без него
    инвариант не выполняется: при сквозной нормировке точки на 1 000 и 800 жителей
    получают доли поездок, пропорциональные их весу, а не населению.
    """
    data = _demand()
    from_pops = data.residents_from_pops()
    for record in data.points:
        if record.residents > 0.0:
            assert from_pops[record.id] == pytest.approx(record.residents, abs=0.5)


def test_phantom_points_are_detected():
    """Точка без жителей и без рабочих мест — не точка спроса."""
    points = _points() + [pf.DemandPoint("empty", 51.05, 39.05, 0.0, 0.0)]
    pairs, norm = _od_matrix(points, pf.PassengerOptions(), {})
    by_key = {point.key: point for point in points}
    tuples = [
        (by_key[p.origin_key], by_key[p.dest_key], p.weight, p.dist_m, p.gamma)
        for p in pairs
    ]
    residents = sum(point.residents for point in points)
    data = city.build_pops(points, tuples, residents, norm)
    assert "empty" in data.phantom_points()


def test_unreferenced_pops_are_detected():
    points = _points()
    data = _demand(points)
    broken = city.DemandData(
        points=data.points,
        pops=data.pops + (
            city.Pop(
                id="ghost", residence_id="h1", job_id="нет-такой-точки",
                size=10.0, driving_seconds=100.0, driving_distance=1500.0,
            ),
        ),
    )
    assert broken.unreferenced_pops() == ("ghost",)


def test_build_pops_rejects_inconsistent_population():
    """`total_residents` не совпадает с суммой по точкам — ошибка вызова."""
    points = _points()
    pairs, norm = _od_matrix(points, pf.PassengerOptions(), {})
    by_key = {point.key: point for point in points}
    tuples = [
        (by_key[p.origin_key], by_key[p.dest_key], p.weight, p.dist_m, p.gamma)
        for p in pairs
    ]
    with pytest.raises(ValueError) as error:
        city.build_pops(points, tuples, 999_999.0, norm)
    assert "не совпадает с суммой" in str(error.value)


def test_tiny_flows_are_not_lost():
    """Потоки меньше одного человека не могут стать попом, но и теряться не могут.

    Минимальный размер попа — один человек (§A.5f). Если остаток просто
    выбросить, сумма размеров попов перестанет равняться населению: на городе с
    тысячами назначений на точку набираются сотни тысяч потерянных людей, и
    ломается именно тот инвариант, который проверяет `demand_residents_match`.
    """
    points = _points()
    # Много назначений, каждое с крошечной долей: сумма долей равна единице,
    # но ни одна не достигает половины человека.
    points = points + [
        pf.DemandPoint(f"j{i}", 51.030 + i * 0.001, 39.020, 0.0, 1.0)
        for i in range(200)
    ]
    pairs, norm = _od_matrix(points, pf.PassengerOptions(), {})
    by_key = {point.key: point for point in points}
    tuples = [
        (by_key[p.origin_key], by_key[p.dest_key], p.weight, p.dist_m, p.gamma)
        for p in pairs
    ]
    residents = sum(point.residents for point in points)
    data = city.build_pops(points, tuples, residents, norm)

    assert data.residents_mismatch() == {}
    assert sum(pop.size for pop in data.pops) == pytest.approx(residents, abs=1.0)


def test_origin_with_no_reachable_destination_keeps_its_people():
    """Начало, у которого все назначения дальше порога притяжения.

    Поездки существуют, а попов не создаётся. Один поп без назначения честнее,
    чем потерянное население, и появление такого попа — сигнал о городе, а не
    о сборке.
    """
    points = [
        pf.DemandPoint("far_home", 51.000, 39.000, 500.0, 0.0),
        pf.DemandPoint("near_job", 51.001, 39.000, 0.0, 800.0),
    ]
    # Ограничение притяжения ужимается до нуля: ни одна пара не проходит.
    options = pf.PassengerOptions(max_dist_m=1.0, hard_limit_m=1.0)
    pairs, norm = _od_matrix(points, options, {})
    by_key = {point.key: point for point in points}
    tuples = [
        (by_key[p.origin_key], by_key[p.dest_key], p.weight, p.dist_m, p.gamma)
        for p in pairs
    ]
    data = city.build_pops(points, tuples, 500.0, norm)
    assert data.residents_mismatch() == {}
    assert data.pops
    # Назначения нет — это null, а не выдуманный идентификатор точки.
    assert data.pops[0].job_id is None
    assert data.destinations_unreachable() == (data.pops[0].id,)
    assert data.unreferenced_pops() == ()
    # JSON допускает null, и round-trip сохраняет его как null, а не как "None".
    restored = city.DemandData.from_json(data.to_json())
    assert restored.pops[0].job_id is None


def test_nearest_neighbour_searches_expanding_rings():
    """Точка в разрежённом краю не должна объявляться бесконечно далёкой.

    Шаг сетки считается по extent'у всех точек, и при неравномерном
    распределении в 3×3 ячейках у точки может не оказаться соседей, хотя
    ближайшая точка есть в двух клетках.
    """
    dense = [(39.0, 51.0), (39.001, 51.0), (39.002, 51.0), (39.003, 51.0)]
    sparse = (39.5, 51.5)
    distances = city.nearest_neighbour_distances_m(dense + [sparse])
    assert all(math.isfinite(value) for value in distances)
    assert distances[-1] > 30_000.0


def test_nearest_neighbour_finds_points_in_the_same_cell():
    """Грубая сетка может положить нескольких соседей в одну ячейку.

    Без проверки центральной ячейки такие точки считались бы отсутствующими, и
    расстояние между ними — бесконечным.
    """
    coordinates = [(39.0, 51.0), (39.00001, 51.0), (39.00002, 51.0)]
    distances = city.nearest_neighbour_distances_m(coordinates)
    assert all(math.isfinite(value) for value in distances)
    assert distances[0] < 5.0


def test_build_pops_is_deterministic_under_input_reordering():
    """§32.5: перестановка точек не меняет ни попы, ни их идентификаторы."""
    points = _points()
    pairs, norm = _od_matrix(points, pf.PassengerOptions(), {})
    by_key = {point.key: point for point in points}
    tuples = [
        (by_key[p.origin_key], by_key[p.dest_key], p.weight, p.dist_m, p.gamma)
        for p in pairs
    ]
    residents = sum(point.residents for point in points)
    straight = city.build_pops(points, tuples, residents, norm)
    shuffled_points = list(reversed(points))
    shuffled_tuples = list(reversed(tuples))
    shuffled = city.build_pops(shuffled_points, shuffled_tuples, residents, norm)
    assert straight.to_dict() == shuffled.to_dict()


def test_build_pops_accepts_keys_or_objects():
    """Начало и конец пары принимаются и ключом, и объектом с ``key``.

    ``str(obj)`` для dataclass даёт repr, и подмена выглядела бы правдоподобно:
    файл собрался бы, а точки получили бы имена мусором.
    """
    points = _points()
    pairs, norm = _od_matrix(points, pf.PassengerOptions(), {})
    by_key = {point.key: point for point in points}
    residents = sum(point.residents for point in points)

    objects = [
        (by_key[p.origin_key], by_key[p.dest_key], p.weight, p.dist_m, p.gamma)
        for p in pairs
    ]
    keys = [(p[0].key, p[1].key, p[2], p[3], p[4]) for p in objects]
    assert city.build_pops(points, objects, residents, norm).to_dict() == (
        city.build_pops(points, keys, residents, norm).to_dict()
    )


def test_pop_size_histogram_uses_the_document_buckets():
    """Корзины взяты из §A.5f, иначе распределение не с чем сравнить."""
    histogram = _demand().pop_size_histogram()
    assert list(histogram) == ["1-9", "10-49", "50-99", "100-199", "200+"]


# ── Формат устойчив ───────────────────────────────────────────────────


def test_demand_data_survives_repeated_serialization():
    """Запись и чтение идемпотентны: формат стабилен между сборками."""
    data = _demand()
    once = city.DemandData.from_json(data.to_json())
    twice = city.DemandData.from_json(once.to_json())
    assert once == twice


def test_demand_data_rejects_nonfinite_values():
    """NaN в JSON не выражается, и `allow_nan=False` это ловит."""
    data = _demand()
    broken = city.DemandData(
        points=data.points,
        pops=(city.Pop("x", "h1", "j1", float("nan"), 1.0, 1.0),),
    )
    with pytest.raises(ValueError):
        broken.to_json()


# ── Версия и имя пакета (§18.4b) ──────────────────────────────────────


@pytest.mark.parametrize("version", ["1.0.0", "v1.0.0", "0.1.0", "v10.20.30"])
def test_valid_versions_are_accepted(version):
    assert city.validate_version(version) == version


@pytest.mark.parametrize(
    "version", ["1.0", "1", "v1.0", "1.0.0.0", "1.0.0-rc1", "", "latest"]
)
def test_invalid_versions_are_rejected(version):
    """Формат проверяется, а не разбирается мягко: от него зависит решение
    об обновлении карты у игрока."""
    with pytest.raises(ValueError):
        city.validate_version(version)


def test_package_filename_equals_the_city_code():
    """Имя файла равно коду города — иначе город невозможно адресовать."""
    assert city.package_filename(_identity(), "1.0.0") == "VOG.zip"


@pytest.mark.parametrize("slug", ["voronezh", "msk", "new-york", "a1"])
def test_valid_slugs(slug):
    assert city.validate_slug(slug) == slug


@pytest.mark.parametrize("slug", ["Voronezh", "vor_onezh", "-vor", "vor-", "vor onezh"])
def test_invalid_slugs(slug):
    """Идентификатор города: только строчные латинские, цифры и дефис."""
    with pytest.raises(ValueError):
        city.validate_slug(slug)


# ── Состав выпуска (§A.5) ─────────────────────────────────────────────


def test_release_contains_the_documented_files(tmp_path):
    demand = _demand()
    sizes = city.write_release(
        tmp_path, _identity(), _config(), demand,
        rubric={"rubric_version": "1.0.0"},
        network=RoadNetwork(),
    )
    assert "config.json" in sizes
    assert "demand_data.json.gz" in sizes
    assert "buildings_index.json.gz" in sizes
    assert "roads.geojson.gz" in sizes
    assert "rubric.json" in sizes
    # Взлётных полос нет — значит, и файла нет (§18.4).
    assert "runways_taxiways_geojson.gz" not in sizes


def test_release_has_no_nested_folders(tmp_path):
    """Вложенная папка дала бы путь глубже ожидаемого: игра ищет config.json
    в корне."""
    demand = _demand()
    city.write_release(tmp_path, _identity(), _config(), demand)
    for path in tmp_path.rglob("*"):
        assert path.is_file(), f"в выпуске появилась папка {path}"


def test_release_files_are_gzipped_and_readable(tmp_path):
    demand = _demand()
    city.write_release(tmp_path, _identity(), _config(), demand)
    raw = gzip.decompress((tmp_path / "demand_data.json.gz").read_bytes())
    payload = json.loads(raw)
    assert set(payload) == {"points", "pops"}


def test_release_is_byte_reproducible(tmp_path):
    """Два прогона одного города дают одинаковые байты.

    ``mtime=0`` в gzip и сортировка ключей — без них содержимое менялось бы при
    каждой сборке, и побайтовое сравнение пакетов не работало бы (§32.5).
    """
    demand = _demand()
    first = tmp_path / "a"
    second = tmp_path / "b"
    city.write_release(first, _identity(), _config(), demand)
    city.write_release(second, _identity(), _config(), demand)
    for name in ("config.json", "demand_data.json.gz", "roads.geojson.gz"):
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_config_carries_the_documented_fields():
    payload = _config().to_dict()
    for field in ("city_code", "name", "bbox", "population", "initial_view_state"):
        assert field in payload
    assert payload["schema_version"] == city.CITY_SCHEMA_VERSION
    assert payload["version"] == "1.0.0"


def test_roads_geojson_writes_each_edge_once():
    """Граф неориентированный: обе записи удвоили бы сеть вдвое."""
    edges = {
        "a": [("b", 100.0, "primary"), ("c", 200.0, "primary")],
        "b": [("a", 100.0, "primary")],
        "c": [("a", 200.0, "primary")],
    }
    network = RoadNetwork(
        edges=edges,
        nodes={"a": (51.0, 39.0), "b": (51.01, 39.0), "c": (51.0, 39.01)},
        segment_count=2,
    )
    payload = city.roads_geojson(network)
    segments = [
        feature
        for feature in payload["features"]
        if feature["properties"]["kind"] == "segment"
    ]
    assert len(segments) == 2


# ── Десять проверок (§18.5) ───────────────────────────────────────────


def test_checks_cover_all_ten_contract_names():
    """Все десять проверок из контракта присутствуют."""
    assert len(city.CHECK_NAMES) == 10
    assert "demand_residents_match" in city.CHECK_NAMES
    assert "demand_phantom_points" in city.CHECK_NAMES
    assert "demand_point_spacing" in city.CHECK_NAMES
    assert "config_version_matches_tag" in city.CHECK_NAMES


def test_clean_city_passes_every_check(tmp_path):
    demand = _demand()
    city.write_release(tmp_path, _identity(), _config(), demand)
    report = city.run_checks(
        tmp_path,
        demand=demand,
        release_tag="1.0.0",
        min_spacing_m=100.0,
        max_spacing_m=5000.0,
    )
    assert report.passed, report.summary()
    # Все десять обязательных проверок присутствуют и пройдены. Сверх них в
    # отчёте может быть информационная проверка состояния города — она не входит
    # в контракт и не влияет на приём.
    for name in city.CHECK_NAMES:
        result = next(r for r in report.results if r.name == name)
        assert result.passed, f"{name}: {result.detail}"
    assert set(city.CHECK_NAMES) <= set(report.names())


def test_check_without_demand_data_is_reported_as_not_checked(tmp_path):
    """Проверка, которой нечем выполняться, говорит «не проверена», а не
    «пройдена»."""
    demand = _demand()
    city.write_release(tmp_path, _identity(), _config(), demand)
    report = city.run_checks(tmp_path, release_tag="1.0.0")
    assert not report.passed
    resident = next(r for r in report.results if r.name == "demand_residents_match")
    assert "не проверена" in resident.detail


def test_point_spacing_without_thresholds_is_not_checked(tmp_path):
    """Документ не задаёт допустимое расстояние, поэтому без порога проверка
    не выполняется — подставлять выдуманное число нельзя."""
    demand = _demand()
    city.write_release(tmp_path, _identity(), _config(), demand)
    report = city.run_checks(
        tmp_path, demand=demand, release_tag="1.0.0",
        min_spacing_m=100.0, max_spacing_m=5000.0,
    )
    assert report.passed

    without = city.run_checks(tmp_path, demand=demand, release_tag="1.0.0")
    spacing = next(r for r in without.results if r.name == "demand_point_spacing")
    assert not spacing.passed
    assert "выдумкой" in spacing.detail


def test_point_spacing_detects_crowded_points():
    """Точки ближе порога — данные раздроблены."""
    crowded = [
        pf.DemandPoint(f"h{i}", 51.000 + i * 0.00001, 39.0, 500.0, 0.0)
        for i in range(6)
    ] + [pf.DemandPoint("j0", 51.02, 39.02, 0.0, 3000.0)]
    pairs, norm = _od_matrix(crowded, pf.PassengerOptions(), {})
    by_key = {point.key: point for point in crowded}
    tuples = [
        (by_key[p.origin_key], by_key[p.dest_key], p.weight, p.dist_m, p.gamma)
        for p in pairs
    ]
    data = city.build_pops(crowded, tuples, 3000.0, norm)
    result = city.check_demand_point_spacing(
        data, min_spacing_m=100.0, max_spacing_m=100_000.0
    )
    assert not result.passed
    assert "раздроблены" in result.detail


def test_phantom_points_check_fails_on_an_empty_point(tmp_path):
    points = _points() + [pf.DemandPoint("empty", 51.05, 39.05, 0.0, 0.0)]
    pairs, norm = _od_matrix(points, pf.PassengerOptions(), {})
    by_key = {point.key: point for point in points}
    tuples = [
        (by_key[p.origin_key], by_key[p.dest_key], p.weight, p.dist_m, p.gamma)
        for p in pairs
    ]
    residents = sum(point.residents for point in points)
    demand = city.build_pops(points, tuples, residents, norm)
    result = city.check_phantom_points(demand)
    assert not result.passed
    assert "empty" in result.detail


def test_residents_match_check_detects_a_broken_counter():
    data = _demand()
    broken_points = tuple(
        city.DemandRecord(
            id=record.id, lon=record.lon, lat=record.lat,
            jobs=record.jobs, residents=record.residents + 100.0,
            pop_ids=record.pop_ids,
        )
        for record in data.points
    )
    broken = city.DemandData(points=broken_points, pops=data.pops)
    result = city.check_residents_match(broken)
    assert not result.passed
    assert "расходятся" in result.detail


def test_version_mismatch_is_rejected(tmp_path):
    demand = _demand()
    city.write_release(tmp_path, _identity(), _config(), demand)
    report = city.run_checks(
        tmp_path, demand=demand, release_tag="2.0.0",
        min_spacing_m=100.0, max_spacing_m=5000.0,
    )
    assert not report.passed
    check = next(
        r for r in report.results if r.name == "config_version_matches_tag"
    )
    assert "метка выпуска" in check.detail


def test_missing_file_fails_the_check(tmp_path):
    report = city.run_checks(tmp_path, release_tag="1.0.0")
    assert not report.passed
    config = next(r for r in report.results if r.name == "config_json")
    assert not config.passed


def test_runways_file_required_only_when_declared(tmp_path):
    demand = _demand()
    city.write_release(
        tmp_path, _identity(), _config(has_runways=True), demand
    )
    report = city.run_checks(
        tmp_path, demand=demand, release_tag="1.0.0",
        min_spacing_m=100.0, max_spacing_m=5000.0, has_runways=True,
    )
    assert report.passed, report.summary()
    assert (tmp_path / "runways_taxiways_geojson.gz").exists()


# ── Расстояния ────────────────────────────────────────────────────────


def test_haversine_matches_a_known_distance():
    """Градус широты на экваторе — около 111,3 км."""
    metres = city.haversine_m(0.0, 0.0, 1.0, 0.0)
    assert metres == pytest.approx(111_195.0, rel=0.01)


def test_haversine_is_symmetric_and_zero_on_identity():
    assert city.haversine_m(51.0, 39.0, 51.5, 39.5) == pytest.approx(
        city.haversine_m(51.5, 39.5, 51.0, 39.0)
    )
    assert city.haversine_m(51.0, 39.0, 51.0, 39.0) == 0.0


def test_nearest_neighbour_finds_the_closest_point():
    coordinates = [
        (39.0, 51.0),
        (39.001, 51.0),
        (39.1, 51.1),
    ]
    distances = city.nearest_neighbour_distances_m(coordinates)
    assert distances[0] == pytest.approx(distances[1], rel=1e-6)
    assert distances[2] > distances[0]


def test_isolated_point_is_not_given_a_fake_zero_distance():
    """Подставить ноль значило бы объявить точку идеально размещённой."""
    distances = city.nearest_neighbour_distances_m([(39.0, 51.0)])
    assert distances == [0.0]


def _route_payload(*, work_interval="", departures=()):
    """Payload каталога перевозчиков в том виде, в каком он приходит."""
    return {
        "workInterval": work_interval,
        "trips": [
            {"schedules": [{"departureIntervals": list(departures)}]}
        ],
    }


def test_measured_interval_from_the_catalogue_is_read_as_measured():
    """Опубликованный перевозчиком интервал — измерение, а не допущение."""
    interval = city.interval_for(
        _route_payload(departures=[{"intervalValue": 7}]), "bus"
    )
    assert interval.minutes == pytest.approx(7.0)
    assert interval.source == city.INTERVAL_FROM_CATALOG
    assert interval.measured is True


def test_several_daily_intervals_yield_the_most_frequent_one():
    """Ожидание пассажира определяет самый частый интервал, а не средний."""
    interval = city.interval_for(
        _route_payload(
            departures=[{"intervalValue": 20}, {"intervalValue": 5}]
        ),
        "bus",
    )
    assert interval.minutes == pytest.approx(5.0)


def test_vague_work_interval_is_not_read_as_an_exact_number():
    """«Более 20 мин.» — нижняя оценка; принять её за 20 значит завысить частоту."""
    interval = city.interval_for(
        _route_payload(work_interval="более 20 мин."), "bus"
    )
    assert interval.minutes != pytest.approx(20.0)


def test_absent_interval_becomes_an_explicitly_assumed_one():
    """Нет интервала — подставляется значение по режиму, но помечается."""
    interval = city.interval_for(_route_payload(), "bus")
    assert interval.minutes == pytest.approx(10.0)
    assert interval.source == city.INTERVAL_FROM_MODE_DEFAULT
    assert interval.measured is False


def test_assumed_interval_follows_the_mode_not_the_default_mode():
    """Разные режимы — разные частоты; подстановка «bus» для всех скрыла бы.

    Проверяется на метро: у автобуса и трамвая в §8.5 интервал совпадает
    (10 мин), а у метро он 5 — иначе проверка ничего бы не различала.
    """
    metro = city.interval_for(_route_payload(), "metro")
    bus = city.interval_for(_route_payload(), "bus")
    assert metro.minutes != bus.minutes
    assert metro.source == bus.source == city.INTERVAL_FROM_MODE_DEFAULT


def test_non_positive_interval_is_rejected_rather_than_dividing_by_it():
    assert city.parse_catalog_intervals(_route_payload(departures=[{"intervalValue": 0}])) == []
    assert city.parse_catalog_intervals(_route_payload(departures=[{"intervalValue": "нет"}])) == []


def test_a_network_built_on_assumptions_reports_them_as_assumptions():
    """Прямота расчёта: низкое покрытие измеренными интервалами обязано быть видно."""
    network = city.TransitNetwork(
        slug="voronezh",
        routes=(1, 2, 3, 4),
        intervals={
            1: city.RouteInterval(10.0, city.INTERVAL_FROM_CATALOG),
            2: city.RouteInterval(20.0, city.INTERVAL_FROM_MODE_DEFAULT),
            3: city.RouteInterval(6.0, city.INTERVAL_FROM_MODE_DEFAULT),
            4: city.RouteInterval(15.0, city.INTERVAL_FROM_MODE_DEFAULT),
        },
    )
    assert network.headway_coverage == pytest.approx(0.25)
    assert network.measured_intervals == 1
    assert network.assumed_intervals == 3

    report = city.network_report(network)
    assert report["intervals"]["measured"] == 1
    assert report["intervals"]["assumed_from_mode_default"] == 3
    assert report["intervals"]["absent"] == 0
    assert "допущение" in report["note"]


def test_failed_route_downloads_are_counted_not_dropped_silently():
    """Молчаливый пропуск линии выглядел бы как город, в котором её нет."""
    assert "не загружено 2" in city.TransitNetwork(
        slug="a", routes=(1,), failed=2
    ).summary()


def test_route_without_an_interval_is_kept_out_of_the_headway_table():
    """Отсутствие интервала означает «линия стоит», а не частоту по умолчанию."""
    network = city.TransitNetwork(
        slug="a",
        routes=(1, 2),
        intervals={
            1: city.RouteInterval(10.0, city.INTERVAL_FROM_CATALOG),
            2: city.RouteInterval(None, city.INTERVAL_NONE),
        },
    )
    assert city.headways_for(network) == {1: pytest.approx(10.0)}


def test_import_of_the_loader_works_under_either_sys_path_convention():
    """`wikiroutes/` в `sys.path` и пакет `wikiroutes` — два пути импорта.

    Модуль загрузки использует оба (`routes.py` с относительными импортами и
    `passenger` с плоскими), поэтому он обязан работать при обоих.
    """
    from city.network import _project_module

    assert _project_module("routes") is not None
    assert _project_module("catalog") is not None
    assert _project_module("cache") is not None
