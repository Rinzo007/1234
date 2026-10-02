from dataclasses import dataclass
from types import SimpleNamespace

import overture.process as process
from overture.cache import LRUCache

@dataclass(frozen=True)
class _Direction:
    signature: str
    coords: tuple[tuple[float, float], tuple[float, float]] = (
        (0.0, 0.0),
        (1.0, 1.0),
    )


@dataclass(frozen=True)
class _Route:
    route_id: int
    directions: tuple[_Direction, ...]
    error: bool = False


def _state(cache):
    ctx = SimpleNamespace(sig="ctx", epsg=32631)
    return process._WorkerState(
        ctx=ctx,
        transformer=None,
        cache=cache,
        write_cache=False,
        cache_lock=None,
        buffer_cache=LRUCache(max_size=4),
        buffer_cache_lock=__import__("threading").RLock(),
    )


def test_route_cache_hit_avoids_rebuilding_geometry(monkeypatch):
    class _Cache:
        def __init__(self):
            self.data = {}

        def get(self, namespace, key):
            return self.data.get((namespace, key))

        def put(self, namespace, key, value):
            self.data[(namespace, key)] = value

    cache = _Cache()
    state = _state(cache)
    route = _Route(42, (_Direction("dir-a"),))
    dir_sig = process._direction_signatures(route)[0]
    import hashlib
    route_sig = hashlib.sha256(dir_sig.encode()).hexdigest()[:16]
    key = process._route_cache_key(state.ctx, route_sig)
    cache.put(
        "overture",
        key,
        {
            "route": {"total_area_m2": 10.0, "corridor_m2": 20.0, "count": 1, "ok": True},
            "directions": {
                "0": {"total_area_m2": 10.0, "corridor_m2": 20.0, "count": 1, "ok": True}
            },
        },
    )

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("cache hit must not rebuild route buffers")

    monkeypatch.setattr(process, "_build_cached_direction_buffer", should_not_run)
    route_stats, directions, entries = process._process_single_route(route, state)
    assert route_stats.ok is True
    assert route_stats.count == 1
    assert directions[(42, 0)].ok is True
    assert entries == {}
