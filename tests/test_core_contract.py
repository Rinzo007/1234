from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np

import overture.core as core
from overture.result import OvertureStatus


@dataclass(frozen=True)
class _Stats:
    total_area_m2: float = 0.0
    corridor_m2: float = 0.0
    count: int = 0
    ok: bool = True


def _valid_kwargs():
    return dict(
        routes=[object()],
        overture_path="dummy.parquet",
        buffer_m=50.0,
        bbox=(52.0, 4.0, 53.0, 5.0),
        city="Amsterdam",
        cache=None,
        release="2026-09-01",
    )


def test_invalid_input_has_explicit_status():
    result = core.compute_overture_result(
        **{**_valid_kwargs(), "buffer_m": 0},
    )
    assert result.status is OvertureStatus.INVALID_INPUT
    assert result.ok is False
    assert result.reason == "invalid_buffer_m"


def test_empty_routes_are_skipped_before_source_resolution():
    result = core.compute_overture_result(
        **{**_valid_kwargs(), "routes": []},
    )
    assert result.status is OvertureStatus.SKIPPED
    assert result.reason == "no_routes"


def test_no_buildings_is_distinct_from_error(monkeypatch):
    monkeypatch.setattr(core, "_resolve_overture_paths", lambda _: ["dummy.parquet"])
    monkeypatch.setattr(core, "_build_pipeline", lambda *args, **kwargs: None)
    result = core.compute_overture_result(**_valid_kwargs())
    assert result.status is OvertureStatus.NO_DATA
    assert result.error is None


def test_partial_status_preserves_successful_route_results(monkeypatch):
    monkeypatch.setattr(core, "_resolve_overture_paths", lambda _: ["dummy.parquet"])

    ctx = SimpleNamespace(sig="test", epsg=32631)
    buildings = np.empty(1, dtype=object)
    monkeypatch.setattr(
        core,
        "_build_pipeline",
        lambda *args, **kwargs: (ctx, buildings, 32631),
    )
    ok = _Stats(total_area_m2=10.0, corridor_m2=20.0, count=1, ok=True)
    failed = _Stats(ok=False)
    monkeypatch.setattr(
        core,
        "_run_sequential",
        lambda *args, **kwargs: (
            {1: ok, 2: failed},
            {(1, 0): ok, (2, 0): failed},
        ),
    )
    result = core.compute_overture_result(**_valid_kwargs(), parallel=False)
    assert result.status is OvertureStatus.PARTIAL
    assert result.ok is True
    assert result.stats[1] == ok
    assert result.stats[2].ok is False
    assert result.meta["failed_items"] == 2


def test_legacy_api_adapts_unified_result(monkeypatch):
    monkeypatch.setattr(core, "_resolve_overture_paths", lambda _: ["dummy.parquet"])
    monkeypatch.setattr(core, "_build_pipeline", lambda *args, **kwargs: None)
    stats, meta, directions = core.compute_overture(**_valid_kwargs())
    assert stats == {}
    assert directions == {}
    assert meta is not None
    assert meta["status"] == "no_data"
