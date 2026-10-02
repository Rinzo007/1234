"""Граница города из зданий Overture (без GHS)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pytest

from wikiroutes import urban_boundary as ub


def test_morphology_keeps_core_and_drops_small_islands():
    frac = np.zeros((40, 40), dtype=np.float64)
    frac[10:24, 10:24] = 0.5  # ядро 14x14 = 49 км²
    frac[30:33, 30:33] = 0.5  # остров 3x3 = 2.25 км² < 25 км²
    mask = ub._apply_urban_morphology(frac, core_threshold=0.10,
                                      mortar_threshold=0.05, min_area_km2=25.0)
    assert mask.dtype == bool
    assert mask[10:24, 10:24].all()
    assert not mask[30:33, 30:33].any()


def test_morphology_empty_grid_gives_empty_mask():
    mask = ub._apply_urban_morphology(
        np.zeros((10, 10)), core_threshold=0.10,
        mortar_threshold=0.05, min_area_km2=25.0)
    assert not mask.any()


def test_morphology_fills_small_hole_and_keeps_large_one():
    # Анклав 4x4 ячейки (2 км) заливается; анклав 25x25 (12.5 км > 6 миль) — нет.
    for hole, filled in ((slice(18, 22), True), (slice(8, 33), False)):
        frac = np.zeros((60, 60), dtype=np.float64)
        frac[5:55, 5:55] = 0.5
        frac[hole, hole] = 0.0
        mask = ub._apply_urban_morphology(
            frac, core_threshold=0.10, mortar_threshold=0.05,
            min_area_km2=25.0)
        assert mask[5:55, 5:55].all() == filled, (hole, filled)
        if not filled:
            assert not mask[hole, hole].any()


def test_gap_merge_rule():
    from shapely.geometry import Point, box

    # Разрыв 600 м < 800 м — смыкаются; 1500 м — нет.
    first = box(0, 0, 1000, 1000)
    near = box(1600, 0, 2600, 1000)
    far = box(2500, 0, 3500, 1000)
    merged = ub._pick_boundary_component([first, near], Point(500, 500))
    assert merged.contains(Point(2000, 500))
    split = ub._pick_boundary_component([first, far], Point(500, 500))
    assert not split.contains(Point(3000, 500))


def test_fraction_grid_assigns_building_area_to_centroid_cell():
    import shapely

    geoms = np.asarray(
        [shapely.box(100.0, 100.0, 200.0, 200.0)], dtype=object)
    frac = ub._building_fraction_grid(geoms, 0.0, 0.0, 4, 4, 500.0)
    assert frac.shape == (4, 4)
    # Центроид (150, 150) -> столбец 0, строка 3 (строки считаются сверху,
    # верх сетки y=2000); площадь 10000 м² / 250000 м².
    assert frac[3, 0] == 10000.0 / 250000.0
    assert frac.sum() == 10000.0 / 250000.0


def test_fraction_grid_north_is_row_zero():
    # Регрессия переворота сетки: северное здание — в строке 0 (rasterio
    # считает строки сверху), южное — в последней строке.
    import shapely

    geoms = np.asarray([
        shapely.box(0.0, 1500.0, 100.0, 1600.0),  # север, центроид y=1550
        shapely.box(0.0, 100.0, 100.0, 200.0),  # юг, центроид y=150
    ], dtype=object)
    frac = ub._building_fraction_grid(geoms, 0.0, 0.0, 4, 4, 500.0)
    assert frac[0, 0] > 0.0
    assert frac[3, 0] > 0.0
    assert frac[1:3, :].sum() == 0.0


def test_boundary_from_synthetic_overture_buildings(tmp_path):
    geopandas = pytest.importorskip("geopandas")
    from shapely.geometry import Point, box

    lon, lat = 37.61, 55.75
    # Плотное ядро 6x6 км: дома 40x40 м через каждые 100 м (~16% застройки).
    boxes = []
    for i in range(60):
        for j in range(60):
            x = lon - 0.048 + i * 0.0016
            y = lat - 0.027 + j * 0.0009
            boxes.append(box(x, y, x + 0.00064, y + 0.00036))
    gdf = geopandas.GeoDataFrame({"geometry": boxes}, crs="EPSG:4326")
    src = tmp_path / "buildings.geoparquet"
    gdf.to_parquet(src)

    boundary = ub.build_urban_boundary(lon, lat, str(src), radius_m=8000.0)
    assert boundary is not None and not boundary.is_empty
    assert boundary.contains(Point(lon, lat))

    from pyproj import Transformer

    utm_zone = int((lon + 180) / 6) + 1
    to_utm = Transformer.from_crs(
        "EPSG:4326", f"EPSG:326{utm_zone:02d}", always_xy=True)
    from shapely.ops import transform as shapely_transform

    area_m2 = shapely_transform(to_utm.transform, boundary).area
    assert area_m2 >= 25.0e6


def test_urban_bbox_matches_boundary_extreme_points(tmp_path):
    geopandas = pytest.importorskip("geopandas")
    from shapely.geometry import Point, box

    lon, lat = 37.61, 55.75
    boxes = []
    for i in range(60):
        for j in range(60):
            x = lon - 0.048 + i * 0.0016
            y = lat - 0.027 + j * 0.0009
            boxes.append(box(x, y, x + 0.00064, y + 0.00036))
    gdf = geopandas.GeoDataFrame({"geometry": boxes}, crs="EPSG:4326")
    src = tmp_path / "buildings.geoparquet"
    gdf.to_parquet(src)

    boundary = ub.build_urban_boundary(lon, lat, str(src), radius_m=8000.0)
    bbox = ub.build_urban_bbox(lon, lat, str(src), radius_m=8000.0)

    assert boundary is not None and not boundary.is_empty
    assert bbox is not None
    assert bbox.geom_type == "Polygon"
    assert bbox.bounds == boundary.bounds
    assert bbox.contains(Point(lon, lat))


def test_boundary_bbox_matches_extreme_points():
    from shapely.geometry import Point, box

    from wikiroutes.urban_boundary import boundary_bbox

    boundary = box(10.0, 20.0, 30.0, 40.0).difference(Point(15.0, 25.0).buffer(1.0))
    bbox = boundary_bbox(boundary)
    assert bbox is not None
    assert bbox.bounds == boundary.bounds
    assert bbox.geom_type == "Polygon"
    assert boundary_bbox(Point(1.0, 2.0)) is None
    assert boundary_bbox(None) is None
