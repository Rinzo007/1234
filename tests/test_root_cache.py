from cache import JsonCache


def test_od_roads_has_larger_default_limit(tmp_path):
    cache = JsonCache(tmp_path)
    payload = {"ways": "x" * (9 * 1024 * 1024)}

    cache.put("od_roads", "almaty", payload)

    assert cache.get("od_roads", "almaty") == payload


def test_regular_cache_kind_keeps_default_limit(tmp_path):
    cache = JsonCache(tmp_path)
    payload = {"value": "x" * (9 * 1024 * 1024)}

    cache.put("catalog", "large", payload)

    assert cache.get("catalog", "large") is None
