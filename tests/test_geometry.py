from types import SimpleNamespace

import numpy as np
import pytest
from shapely.geometry import Polygon

from overture.geometry import _intersection_union_area_and_count


def _ctx(geometries):
    return SimpleNamespace(
        polygon_geometries=np.asarray(geometries, dtype=object),
        assume_no_overlap=False,
        use_coverage_union=False,
        union_grid_size=None,
    )


def test_count_matches_positive_area_intersections():
    # First building overlaps the buffer, second only touches its edge,
    # third is disjoint. Only the first one contributes to count and area.
    buildings = [
        Polygon([(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)]),
        Polygon([(1.0, 0.2), (1.5, 0.2), (1.5, 0.8), (1.0, 0.8)]),
        Polygon([(2.0, 0.2), (2.5, 0.2), (2.5, 0.8), (2.0, 0.8)]),
    ]
    buffer = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    area, had_error, count = _intersection_union_area_and_count(
        buffer, np.arange(3, dtype=np.intp), _ctx(buildings)
    )
    assert area > 0
    assert had_error is False
    assert count == 1
