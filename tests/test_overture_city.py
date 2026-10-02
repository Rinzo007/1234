"""Тесты загрузки города из Overture (``wikiroutes.overture.city``).

Сеть не используется: проверяются решения, которые обязаны быть верны
независимо от данных, — порядок координат, разбор схемы, устройство графа и
детерминизм манифеста.
"""

from __future__ import annotations

import math

import pytest


from overture.city import (
    ABSENT_THEMES,
    CATALOG_ONLY_FIELDS,
    GAME_MANIFEST_FIELDS,
    REGION_COUNT,
    SPECIAL_DEMAND,
    CityIdentity,
    CityPackage,
    CityProvenance,
    LatLonBBox,
    RoadNetwork,
    _field,
    _length_m,
    _none_if_nan,
    boundary_from_buildings,
    build_road_network,
    load_boundary_from_geojson,
    part_urls,
    places_frame,
)

VORONEZH = LatLonBBox(
    min_lat=51.51604303595857,
    min_lon=38.97920153982201,
    max_lat=51.81958334407018,
    max_lon=39.4345928140973,
)


# ── Порядок координат ─────────────────────────────────────────────────


def test_bbox_roundtrips_through_both_conventions():
    box = LatLonBBox(min_lat=51.5, min_lon=39.0, max_lat=51.8, max_lon=39.4)
    assert box.as_project_tuple() == (51.5, 39.0, 51.8, 39.4)
    assert box.as_xy() == (39.0, 51.5, 39.4, 51.8)
    assert LatLonBBox.from_xy(box.as_xy()) == box
    assert LatLonBBox.from_project_tuple(box.as_project_tuple()) == box


def test_bbox_rejects_inverted_corners():
    """Отказ при ``min > max`` — единственная защита от перестановки осей.

    Чего защита **не** ловит и ловить не может: согласованную перестановку
    широты и долготы. ``(lat 39,0..39,4; lon 51,5..51,8)`` — корректный, но
    неверный город, и никакой проверкой порядка от него не спастись. Именно
    поэтому в ``LatLonBBox`` поля названы, а не передаются кортежем.
    """
    with pytest.raises(ValueError) as error:
        LatLonBBox(min_lat=51.8, min_lon=39.0, max_lat=51.5, max_lon=39.4)
    assert "перепутаны" in str(error.value)


def test_bbox_consistent_swap_is_not_detectable_and_says_so():
    """Согласованная перестановка даёт валидный, но другой город.

    Тест фиксирует границу возможностей проверки, чтобы на неё не
    опирались: отказ при min > max не является защитой от перепутанных осей.
    """
    swapped = LatLonBBox(min_lat=39.0, min_lon=51.5, max_lat=39.4, max_lon=51.8)
    assert swapped.as_project_tuple() == (39.0, 51.5, 39.4, 51.8)
    assert swapped != LatLonBBox(
        min_lat=51.5, min_lon=39.0, max_lat=51.8, max_lon=39.4
    )


def test_bbox_rejects_nonfinite():
    with pytest.raises(ValueError):
        LatLonBBox(
            min_lat=float("nan"), min_lon=39.0, max_lat=51.8, max_lon=39.4
        )


def test_bbox_contains_and_area():
    box = LatLonBBox(min_lat=51.5, min_lon=39.0, max_lat=51.6, max_lon=39.1)
    assert box.contains(51.55, 39.05)
    assert not box.contains(52.0, 39.05)
    # Примерно 6.2 км²: 0,1° широты ≈ 11,06 км, 0,1° долготы на 51° ≈ 6,9 км.
    assert 30.0 < box.area_km2 < 100.0
    assert "lat 51.5000" in box.summary()


def test_voronezh_bbox_project_order_is_lat_first():
    """Именно тот порядок, который ждут функции Overture."""
    values = VORONEZH.as_project_tuple()
    assert values[0] == pytest.approx(51.516, abs=1e-3)
    assert values[1] == pytest.approx(38.979, abs=1e-3)


# ── Чтение полей ──────────────────────────────────────────────────────


class _Record:
    """Запись в стиле ``itertuples`` — то, что реально приходит из GeoPandas.

    Поля задаются словарём, а не именованными аргументами: одно из них —
    ``class``, а это зарезервированное слово Python.
    """

    def __init__(self, fields=None, **extra):
        for key, value in {**(fields or {}), **extra}.items():
            setattr(self, key, value)


def test_field_distinguishes_missing_from_falsy():
    assert _field(_Record(), "class", "") == ""
    assert _field(_Record({"class": None}), "class", "") == ""
    assert _field(_Record({"class": float("nan")}), "class", "") == ""
    assert _field(_Record({"class": "primary"}), "class", "") == "primary"


def test_field_does_not_apply_truthiness_to_arrays():
    """``or`` на numpy-массиве падает — и падал бы на втором сегменте."""
    import numpy as np

    connectors = np.array([("a", 0.0), ("b", 1.0)], dtype=object)
    value = _field(_Record({"connectors": connectors}), "connectors", ())
    assert len(value) == 2
    assert _field(
        _Record({"connectors": np.array([], dtype=object)}), "connectors", ()
    ) == ()


def test_none_if_nan_only_touches_nan():
    assert _none_if_nan(float("nan")) is None
    assert _none_if_nan("nan") == "nan"
    assert _none_if_nan("shopping") == "shopping"
    assert _none_if_nan(0.0) == 0.0


# ── Длина в метрах ────────────────────────────────────────────────────


def test_length_is_metres_not_degrees():
    """``geometry.length`` в CRS84 — это градусы, и подставить их как метры
    значит подставить число другой величины без единого признака."""
    from shapely.geometry import LineString

    # Отрезок вдоль меридиана: 0,01° широты ≈ 1106 м, а в градусах 0,01.
    line = LineString([(39.0, 51.50), (39.0, 51.51)])
    degrees = float(line.length)
    metres = _length_m(line)

    assert degrees == pytest.approx(0.01, abs=1e-9)
    assert 1050.0 < metres < 1160.0
    # Отношение обязано совпадать с cos(широты), иначе это не метры.
    assert metres / degrees == pytest.approx(111_320.0, rel=0.02)


def test_length_of_empty_or_missing_geometry_is_zero():
    from shapely.geometry import LineString

    assert _length_m(None) == 0.0
    assert _length_m(LineString()) == 0.0


# ── Дорожный граф ─────────────────────────────────────────────────────


def _frame(rows, columns):
    import pandas as pd

    return pd.DataFrame(rows, columns=columns)


def _connector_record(identifier, lat, lon):
    from shapely.geometry import Point

    return _Record(id=identifier, geometry=Point(lon, lat))


def _segment_record(identifier, klass, connectors, lat0, lon0, lat1, lon1):
    from shapely.geometry import LineString

    return _Record(
        id=identifier,
        **{
            "class": klass,
            "connectors": connectors,
            "geometry": LineString([(lon0, lat0), (lon1, lat1)]),
        },
    )


class _Frame(list):
    """Список записей с ``itertuples`` — достаточно для построения графа."""

    def itertuples(self):
        return iter(self)


def test_road_network_splits_segments_at_every_connector():
    """Промежуточный коннектор — это узел, а не потерянный перекрёсток.

    Если взять только крайние коннекторы сегмента, промежуточные останутся
    узлами степени 0 — граф будет выглядеть дырявым, хотя данные в порядке.
    """
    connectors = _Frame([
        _connector_record("c1", 51.50, 39.00),
        _connector_record("c2", 51.505, 39.00),
        _connector_record("c3", 51.51, 39.00),
    ])
    segments = _Frame([
        _segment_record(
            "s1", "residential",
            [{"connector_id": "c1", "at": 0.0},
             {"connector_id": "c2", "at": 0.5},
             {"connector_id": "c3", "at": 1.0}],
            51.50, 39.00, 51.51, 39.00,
        )
    ])

    network = build_road_network(segments, connectors)
    assert network.node_count == 3
    assert network.degree_histogram().get(0, 0) == 0
    # Три узла в линию: два крайних степени 1, средний степени 2.
    assert network.degree_histogram() == {1: 2, 2: 1}


def test_road_network_divides_length_by_connector_position():
    """Длина ребра — часть длины сегмента, пропорциональная ``at``."""
    connectors = _Frame([
        _connector_record("c1", 51.50, 39.00),
        _connector_record("c2", 51.505, 39.00),
        _connector_record("c3", 51.51, 39.00),
    ])
    segments = _Frame([
        _segment_record(
            "s1", "residential",
            [{"connector_id": "c1", "at": 0.0},
             {"connector_id": "c2", "at": 0.25},
             {"connector_id": "c3", "at": 1.0}],
            51.50, 39.00, 51.51, 39.00,
        )
    ])
    network = build_road_network(segments, connectors)

    def edge_length(node: str, neighbour: str) -> float:
        for other, length, _klass in network.neighbours(node):
            if other == neighbour:
                return length
        raise AssertionError(f"нет ребра {node}→{neighbour}")

    # at = 0, 0.25, 1 → первая четверть пути и остальные три четверти.
    first = edge_length("c1", "c2")
    second = edge_length("c2", "c3")
    assert first == pytest.approx(second * 0.25 / 0.75, rel=1e-6)


def test_road_network_ignores_segments_without_connectors():
    connectors = _Frame([_connector_record("c1", 51.5, 39.0)])
    segments = _Frame([
        _segment_record("s1", "primary", [], 51.5, 39.0, 51.6, 39.0),
    ])
    network = build_road_network(segments, connectors)
    assert network.segment_count == 0
    assert network.node_count == 1


def test_road_network_filters_by_class():
    connectors = _Frame([
        _connector_record("c1", 51.5, 39.0),
        _connector_record("c2", 51.6, 39.0),
    ])
    segments = _Frame([
        _segment_record(
            "s1", "residential",
            [{"connector_id": "c1", "at": 0.0}, {"connector_id": "c2", "at": 1.0}],
            51.5, 39.0, 51.6, 39.0,
        )
    ])
    assert build_road_network(
        segments, connectors, keep_classes=frozenset({"primary"})
    ).segment_count == 0
    kept = build_road_network(
        segments, connectors, keep_classes=frozenset({"residential"})
    )
    assert kept.segment_count == 1
    assert kept.neighbours("c1")[0][2] == "residential"


def test_road_network_of_missing_themes_is_empty_not_an_error():
    network = build_road_network(None, None)
    assert network.node_count == 0
    assert network.segment_count == 0


def test_road_network_edges_are_undirected():
    connectors = _Frame([
        _connector_record("c1", 51.5, 39.0),
        _connector_record("c2", 51.6, 39.0),
    ])
    segments = _Frame([
        _segment_record(
            "s1", "primary",
            [{"connector_id": "c1", "at": 0.0}, {"connector_id": "c2", "at": 1.0}],
            51.5, 39.0, 51.6, 39.0,
        )
    ])
    network = build_road_network(segments, connectors)
    assert len(network.neighbours("c1")) == len(network.neighbours("c2")) == 1
    assert network.neighbours("c1")[0][0] == "c2"


# ── Схема мест ────────────────────────────────────────────────────────


def test_places_frame_reads_new_schema_fields():
    """В релизе 2026-09-23.1 у ``place`` нет ``categories`` — есть
    ``basic_category`` и ``taxonomy``."""
    import pandas as pd

    frame = pd.DataFrame(
        {
            "id": ["a", "b"],
            "geometry": [None, None],
            "basic_category": ["shopping", "grocery_store"],
            "names": [{"primary": "Магазин"}, {"primary": "Магнит"}],
            "taxonomy": [{"primary": "retail"}, {"primary": "food"}],
        }
    )
    result = places_frame(frame)
    assert list(result["name"]) == ["Магазин", "Магнит"]
    assert list(result["basic_category"]) == ["shopping", "grocery_store"]
    assert list(result["taxonomy_primary"]) == ["retail", "food"]


def test_places_frame_keeps_missing_category_distinguishable():
    """``None`` и ``NaN`` — разные вещи: «категории нет» и «прочиталось NaN».

    Иначе пропуск попадёт в γ показателя (§5.5) как настоящая категория.
    """
    import numpy as np
    import pandas as pd

    frame = pd.DataFrame(
        {
            "id": ["a", "b", "c"],
            "geometry": [None, None, None],
            "basic_category": ["shopping", np.nan, None],
            "names": [{"primary": "А"}, {"primary": "Б"}, {"primary": None}],
            "taxonomy": [{"primary": "retail"}, {"primary": np.nan}, None],
        }
    )
    result = places_frame(frame)
    assert result["basic_category"][1] is None
    assert result["basic_category"][2] is None
    assert result["taxonomy_primary"][1] is None
    assert result["taxonomy_primary"][2] is None
    # Присваивание None в float-колонку вернуло бы NaN — это проверяется
    # именно на типе, а не на печати.
    assert all(
        value is None for value in result["basic_category"].tolist()[1:]
    )


def test_places_frame_falls_back_to_common_name():
    import pandas as pd

    frame = pd.DataFrame(
        {
            "id": ["a"],
            "geometry": [None],
            "names": [{"primary": None, "common": {"ru": "Кафе", "en": "Cafe"}}],
        }
    )
    # Порядок map в parquet не задан: имя берётся по отсортированному ключу,
    # иначе пакет города зависел бы от порядка словаря (§32.5).
    assert places_frame(frame)["name"][0] == "Cafe"


def test_places_frame_falls_back_to_legacy_categories():
    import pandas as pd

    frame = pd.DataFrame(
        {
            "id": ["a"],
            "geometry": [None],
            "names": [{"primary": "А"}],
            "categories": [["historic", "tourism"]],
        }
    )
    assert places_frame(frame)["basic_category"][0] == "historic"


# ── Части и URL ───────────────────────────────────────────────────────


def test_part_urls_does_not_duplicate_the_bucket():
    """Ключ Overture начинается с имени бакета.

    Наивная склейка ``host + "/" + key`` дала бы путь с бакетом дважды и 404
    без внятной причины — так и вышло при первой попытке.
    """
    import urllib.parse

    key = "overturemaps-us-west-2/release/2026-09-23.1/theme=x/type=y/part.parquet"
    url = part_urls((key,))[0]
    parts = urllib.parse.urlsplit(url)
    # Бакет не дублируется в пути: он живёт в имени хоста, а путь начинается с
    # ``/release/``. Склейка ``host + "/" + key`` дала бы путь с бакетом дважды
    # и 404 без внятной причины.
    assert parts.netloc.startswith("overturemaps-us-west-2.")
    assert parts.path == "/release/2026-09-23.1/theme=x/type=y/part.parquet"
    assert "/overturemaps-us-west-2/" not in url


def test_part_urls_of_empty_list_is_empty():
    assert part_urls(()) == []


# ── Манифест ──────────────────────────────────────────────────────────


def _package(**kwargs) -> CityPackage:
    return CityPackage(
        slug="test",
        bbox=VORONEZH,
        boundary=None,
        buildings=None,
        places=None,
        road_network=RoadNetwork(),
        provenance=CityProvenance(release="2026-09-23.1", **kwargs),
    )


def test_manifest_is_deterministic_and_has_no_timestamp():
    """§32.5: пакет города обязан быть побайтово воспроизводимым.

    Дата загрузки сделала бы два одинаковых города неразличимыми, а хеш
    содержимого — различимыми.
    """
    first = _package(themes={"places": 10}).manifest()
    second = _package(themes={"places": 10}).manifest()
    assert first == second
    assert first["content_hash"] == second["content_hash"]
    assert "timestamp" not in str(first).lower()
    assert "2026-09-23.1" in str(first)


def test_manifest_hash_changes_with_content():
    a = _package(themes={"places": 10}).manifest()["content_hash"]
    b = _package(themes={"places": 11}).manifest()["content_hash"]
    assert a != b


def test_manifest_records_absent_themes_and_sources():
    """Чего в Overture нет — тоже часть манифеста, а не молчание."""
    manifest = _package().manifest()
    provenance = manifest["provenance"]
    assert provenance["absent_themes"] == [
        f"{theme}/{otype}" for theme, otype in ABSENT_THEMES
    ]
    assert "не в Overture" in provenance["transit_source"]
    assert "не в Overture" in provenance["population_source"]


def test_provenance_is_ordered_deterministically():
    provenance = CityProvenance(
        release="r",
        themes={"b": 2, "a": 1},
        parts={"z": ("p",), "y": ("q",)},
    )
    payload = provenance.to_dict()
    assert list(payload["themes"]) == ["a", "b"]
    assert list(payload["parts"]) == ["y", "z"]
    # Хеш не должен зависеть от порядка вставки в словари.
    other = CityProvenance(
        release="r", themes={"a": 1, "b": 2}, parts={"y": ("q",), "z": ("p",)}
    )
    assert provenance.content_hash() == other.content_hash()


# ── Граница ───────────────────────────────────────────────────────────


def test_boundary_from_buildings_needs_buildings():
    assert boundary_from_buildings(None) is None
    import pandas as pd

    assert boundary_from_buildings(pd.DataFrame({"geometry": []})) is None


def test_boundary_from_geojson(tmp_path):
    import json

    path = tmp_path / "boundary.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [39.0, 51.5],
                                    [39.4, 51.5],
                                    [39.4, 51.8],
                                    [39.0, 51.8],
                                    [39.0, 51.5],
                                ]
                            ],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    boundary = load_boundary_from_geojson(str(path))
    assert boundary is not None
    assert boundary.bounds == (39.0, 51.5, 39.4, 51.8)
    # Shapely считает площадь в единицах СКП, а СКП здесь — градусы: 0,12
    # квадратных градуса, а не метры. Это важно для отчёта о городе: подставить
    # вместо этого «0,12 км²» значит занизить площадь в 7,6 миллиона раз.
    assert boundary.area == pytest.approx(0.4 * 0.3, rel=1e-9)
    # Для метров площадь считается по cos(широты), и она около 916 км².
    mean_lat = math.radians(51.65)
    square_km = 0.4 * 111.32 * math.cos(mean_lat) * 0.3 * 110.574
    assert square_km == pytest.approx(916.0, rel=0.02)


def test_boundary_from_empty_geojson_fails_loudly(tmp_path):
    import json

    path = tmp_path / "empty.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": []}),
                    encoding="utf-8")
    with pytest.raises(ValueError):
        load_boundary_from_geojson(str(path))


# ── Игровой манифест (§18.4, §18.4a, §18.4c) ─────────────────────────


def _city_identity(**kwargs) -> CityIdentity:
    fields = {
        "city_code": "VOG",
        "name": "Воронеж",
        "country": "RU",
        "region": "Европа",
        "population": 1_234_573.0,
        "data_source": "GHSL GHS_POP E2030 100m + Overture 2026-09-23.1",
        "latitude": 51.668,
        "longitude": 39.200,
    }
    fields.update(kwargs)
    return CityIdentity(**fields)


def _voronezh_quality():
    import rubric as rb

    return rb.evaluate_rubric(
        jobs_basis="estimated_proxy", jobs_resolution="mesh_250",
        jobs_intensity="size_only", jobs_granularity="mesh_125",
        residents_basis="total_population", residents_resolution="mesh_250",
        residents_intensity="size_only", residents_granularity="mesh_125",
        od_metric="none", od_granularity="mesh_125",
    )


def test_game_manifest_carries_all_seven_game_fields():
    manifest = _package().game_manifest(_city_identity(), _voronezh_quality())
    assert len(GAME_MANIFEST_FIELDS) == 7
    for field in GAME_MANIFEST_FIELDS:
        assert field in manifest, f"в игровом манифесте нет поля {field}"


def test_game_manifest_excludes_catalog_only_fields():
    """Поля каталога на 406 городов в игровой манифест не попадают.

    `source_quality` и `level_of_detail` исключены не по недосмотру: они авторская
    самооценка, которая в источнике противоречит вычисленному уровню, и §18.4a
    запрещает показывать их рядом с ним без пометки о происхождении.
    """
    manifest = _package().game_manifest(_city_identity(), _voronezh_quality())
    for field in CATALOG_ONLY_FIELDS:
        assert field not in manifest, f"поле каталога {field} попало в игру"


def test_game_manifest_quality_is_computed_not_declared():
    """§18.5: уровень не может быть заявлен автором.

    Манифест принимает объект рубрики и берёт из него вычисленные значения; строки
    с уровнем на входе нет вовсе.
    """
    manifest = _package().game_manifest(_city_identity(), _voronezh_quality())
    block = manifest["data_quality"]
    assert block["origin"] == "computed"
    assert block["level"] == "very-low"
    assert block["rubric_version"] == "1.0.0"
    assert block["weighted_score"] == pytest.approx(0.235312, abs=1e-6)
    # Строкового «уровня» рядом нет: единственный источник — блок с пометкой.
    assert "level" not in {
        key for key in manifest if key != "data_quality"
    }


def test_game_manifest_marks_unevaluated_quality_as_unknown():
    """``unknown`` — «не оценивали», и это не то же, что ``absent``."""
    manifest = _package().game_manifest(_city_identity(), None)
    block = manifest["data_quality"]
    assert block["level"] == "unknown"
    assert block["rubric_version"] is None
    assert block["weighted_score"] is None
    assert "не вычислялась" in block["note"]


def test_game_manifest_does_not_invent_a_pass_on_unrun_check():
    """Невыполненная проверка пишется как ``None``, а не как «сошлось».

    ``demand_residents_match`` — одна из десяти обязательных проверок (§18.5).
    Значение ``True`` по умолчанию означало бы «инвариант проверен», а он не
    проверен.
    """
    manifest = _package().game_manifest(_city_identity(), _voronezh_quality())
    assert manifest["demand_residents_match"] is None

    checked = _package().game_manifest(
        _city_identity(), _voronezh_quality(), residents_match=True
    )
    assert checked["demand_residents_match"] is True


def test_game_manifest_city_key_is_a_pair():
    """§18.4c: ключ города — пара ``(country, city_code)``.

    Трёхбуквенный код — сокращение названия, а не идентификатор: в реестре
    два разных `DAY` в США, и объединение по коду показало бы игроку чужую карту.
    """
    manifest = _package().game_manifest(_city_identity(), _voronezh_quality())
    assert manifest["city_key"] == ["RU", "VOG"]
    assert _city_identity().key == ("RU", "VOG")


def test_identity_rejects_a_lowercase_city_code():
    with pytest.raises(ValueError) as error:
        _city_identity(city_code="vog")
    assert "ЗАГЛАВНЫМИ" in str(error.value)


def test_identity_requires_a_country_because_it_is_part_of_the_key():
    with pytest.raises(ValueError):
        _city_identity(country="")


def test_identity_rejects_unknown_special_demand():
    """Семь значений названы в документе; выдуманное значение — ошибка."""
    assert len(SPECIAL_DEMAND) == 7
    with pytest.raises(ValueError) as error:
        _city_identity(special_demand=("аэропорт",))
    assert "неизвестный специальный спрос" in str(error.value)


def test_region_count_is_declared_but_the_list_is_not_fabricated():
    """Документ называет число регионов, но не перечисляет их.

    Поэтому поле принимается строкой без проверки принадлежности: проверять по
    выдуманному списку значило бы отклонять города по несуществующему правилу.
    """
    assert REGION_COUNT == 19
    identity = _city_identity(region="Азия")
    assert identity.region == "Азия"


def test_initial_view_state_is_a_nested_object():
    """§18.4a: вложенный объект с обязательными широтой, долготой и масштабом."""
    state = _city_identity().initial_view_state()
    assert set(state) == {"latitude", "longitude", "zoom"}
    manifest = _package().game_manifest(_city_identity(), _voronezh_quality())
    assert manifest["initial_view_state"] == state


def test_game_manifest_is_json_serialisable_and_stable():
    import json

    manifest = _package().game_manifest(_city_identity(), _voronezh_quality())
    assert json.loads(json.dumps(manifest, ensure_ascii=False)) == manifest
    again = _package().game_manifest(_city_identity(), _voronezh_quality())
    assert manifest == again