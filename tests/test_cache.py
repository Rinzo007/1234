import os
from dataclasses import dataclass

from overture.cache import (
    LRUCache,
    _file_signature,
    _route_stats_from_cached,
    _stats_from_cached,
    _stats_to_cached,
)

@dataclass(frozen=True)
class _Stats:
    total_area_m2: float = 0.0
    corridor_m2: float = 0.0
    count: int = 0
    ok: bool = True


def test_lru_cache_evicts_least_recently_used():
    cache = LRUCache(max_size=2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1
    cache.put("c", 3)
    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3


def test_stats_round_trip():
    original = _Stats(total_area_m2=12.5, corridor_m2=30.0, count=4, ok=True)
    restored = _stats_from_cached(_stats_to_cached(original))
    assert restored.total_area_m2 == 12.5
    assert restored.corridor_m2 == 30.0
    assert restored.count == 4
    assert restored.ok is True


def test_route_cache_parser_rejects_incomplete_payload():
    assert _route_stats_from_cached({"route": {}}) is None


def test_file_signature_uses_content_not_only_size_and_mtime(tmp_path):
    path = tmp_path / "source.parquet"
    path.write_bytes(b"abcd")
    signature_before = _file_signature([str(path)])
    stat = path.stat()
    path.write_bytes(b"wxyz")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    signature_after = _file_signature([str(path)])
    assert signature_before != signature_after
