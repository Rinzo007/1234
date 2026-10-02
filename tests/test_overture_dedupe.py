"""Векторизованная дедупликация Overture-загрузчика: семантика построчного пути."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np


def test_merge_chunk_dup_winner_and_anonymous_ids():
    geopandas = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    from wikiroutes.overture.load import _merge_chunk_into_dedup

    gdf = geopandas.GeoDataFrame(
        {
            "geometry": [Point(i, 0) for i in range(6)],
            "id": ["a", "a", None, float("nan"), "", "b"],
            "version": [1.0, 2.0, 0.0, 0.0, 0.0, float("inf")],
        },
        crs="EPSG:4326",
    )
    by_id: dict = {}
    anonymous: list = []
    _merge_chunk_into_dedup(gdf, by_id=by_id, anonymous=anonymous)
    # Побеждает максимальная версия; inf -> -1 через _coerce_version.
    assert by_id["a"][0] == 2.0
    assert by_id["b"][0] == -1.0
    # None / nan / '' — анонимные (nan больше не склеивается в ключ 'nan').
    assert len(anonymous) == 3
    assert "nan" not in by_id


def test_hash_dedupe_keeps_first_and_order():
    from shapely.geometry import box

    from wikiroutes.overture.load import _dedupe_geometries_by_hash

    a = box(0, 0, 1, 1)
    b = box(2, 2, 3, 3)
    arr = np.asarray([a, b, a, b, a], dtype=object)
    result = _dedupe_geometries_by_hash(arr)
    assert len(result) == 2
    assert result[0].equals(a) and result[1].equals(b)


import pytest  # noqa: E402
